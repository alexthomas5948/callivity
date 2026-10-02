"""
Callivity Voice AI SaaS Backend - FastAPI Webhook Receiver & Billing Ledger
Receives LiveKit Webhooks and logs billable call minutes & recordings into Supabase.

Events Handled:
1. 'room_finished':
   - Calculates duration_seconds = end_time - start_time
   - Extracts caller phone number from room metadata or participants
   - Inserts call record into Supabase 'call_records' table
2. 'egress_ended':
   - Extracts S3/GCP audio recording URL from egress_info
   - Updates corresponding row in 'call_records' table
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional, Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Header, status
from fastapi.responses import JSONResponse
from supabase import create_client, Client
from livekit import api

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("callivity-webhook")

# Initialize FastAPI application
app = FastAPI(
    title="Callivity Voice AI Webhook & Billing Ledger",
    description="LiveKit Webhook Receiver logging billable minutes and egress recordings into Supabase",
    version="1.0.0",
)

# Environment configuration
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")

if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
    logger.warning("LIVEKIT_API_KEY or LIVEKIT_API_SECRET is missing. Webhook verification will fail!")

if not SUPABASE_URL or not SUPABASE_KEY:
    logger.warning("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is missing. Database operations will fail!")

# Initialize Supabase client
supabase: Client = create_client(SUPABASE_URL or "https://placeholder.supabase.co", SUPABASE_KEY or "placeholder-key")

# Initialize LiveKit WebhookReceiver for cryptographic HMAC signature verification
token_verifier = api.TokenVerifier(
    api_key=LIVEKIT_API_KEY or "",
    api_secret=LIVEKIT_API_SECRET or ""
)
webhook_receiver = api.WebhookReceiver(token_verifier)


def extract_caller_phone_from_event(event_room: Any) -> str:
    """
    Extracts the caller phone number from LiveKit room metadata or properties.
    """
    metadata = getattr(event_room, "metadata", "")
    if metadata:
        try:
            parsed = json.loads(metadata) if isinstance(metadata, str) else metadata
            if isinstance(parsed, dict):
                for key in ["caller_phone", "phone_number", "phone", "from", "caller_id", "telephone"]:
                    val = parsed.get(key)
                    if val:
                        return str(val).strip()
        except Exception:
            if isinstance(metadata, str) and (metadata.startswith("+") or metadata.isdigit()):
                return metadata.strip()

    # Fallback: check if room name encodes the phone number
    room_name = getattr(event_room, "name", "")
    if room_name and ("+91" in room_name or room_name.startswith("sip_")):
        parts = room_name.split("_")
        for part in parts:
            if part.startswith("+") or (part.isdigit() and len(part) >= 10):
                return part

    return "+91-UNKNOWN"


def extract_recording_url(egress_info: Any) -> Optional[str]:
    """
    Extracts the S3, GCP, or HTTP audio recording URL from the EgressInfo object.
    """
    # 1. Check file_results array (standard in composite and room egress)
    file_results = getattr(egress_info, "file_results", None)
    if file_results:
        for file_info in file_results:
            loc = getattr(file_info, "location", None) or getattr(file_info, "filename", None)
            if loc:
                return str(loc)

    # 2. Check singular file attribute
    file_info = getattr(egress_info, "file", None)
    if file_info:
        loc = getattr(file_info, "location", None) or getattr(file_info, "filename", None)
        if loc:
            return str(loc)

    # 3. Check segments_info (for HLS / segmented egress)
    segments_info = getattr(egress_info, "segments_info", None)
    if segments_info:
        playlist_loc = getattr(segments_info, "playlist_location", None)
        if playlist_loc:
            return str(playlist_loc)

    return None


@app.get("/health")
async def health_check():
    """Health check endpoint for cloud monitoring and load balancers."""
    return {
        "status": "healthy",
        "service": "callivity-webhook-receiver",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@app.post("/livekit/webhook")
async def handle_livekit_webhook(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    """
    Validates LiveKit webhook cryptographic signatures and processes:
    - room_finished: Logs call timestamps, duration, and phone number to Supabase.
    - egress_ended: Updates call record with S3/GCP audio recording URL.
    """
    if not authorization:
        logger.warning("Rejected webhook request: Missing Authorization header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header"
        )

    # LiveKit WebhookReceiver requires the raw request body string for HMAC validation
    raw_body = await request.body()
    body_str = raw_body.decode("utf-8")

    try:
        event = webhook_receiver.receive(body_str, authorization)
    except Exception as exc:
        logger.error(f"Webhook signature verification failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid webhook signature: {str(exc)}"
        )

    event_type = getattr(event, "event", None)
    logger.info(f"Received LiveKit webhook event: {event_type} (ID: {getattr(event, 'id', 'N/A')})")

    # ---------------------------------------------------------
    # Event 1: room_finished -> Log Call Duration & Billing
    # ---------------------------------------------------------
    if event_type == "room_finished":
        room = getattr(event, "room", None)
        if not room:
            logger.warning("room_finished event received without room details.")
            return JSONResponse({"status": "ignored", "reason": "No room details"}, status_code=200)

        room_name = getattr(room, "name", "unknown_room")
        creation_time_s = getattr(room, "creation_time", 0)

        # Determine timestamps
        event_created_at_s = getattr(event, "created_at", None)
        if not event_created_at_s:
            event_created_at_s = int(datetime.now(timezone.utc).timestamp())

        start_time_s = creation_time_s if creation_time_s > 0 else event_created_at_s
        end_time_s = event_created_at_s
        duration_seconds = max(0, int(end_time_s - start_time_s))

        start_time_iso = datetime.fromtimestamp(start_time_s, tz=timezone.utc).isoformat()
        end_time_iso = datetime.fromtimestamp(end_time_s, tz=timezone.utc).isoformat()
        caller_phone = extract_caller_phone_from_event(room)

        logger.info(
            f"Processing room_finished for {room_name}: "
            f"Caller={caller_phone}, Duration={duration_seconds}s, "
            f"Start={start_time_iso}, End={end_time_iso}"
        )

        record_payload = {
            "room_name": room_name,
            "caller_phone": caller_phone,
            "start_time": start_time_iso,
            "end_time": end_time_iso,
            "duration_seconds": duration_seconds,
        }

        try:
            # Upsert into Supabase based on unique room_name
            # Preserves existing recording_url if egress_ended arrived earlier
            response = supabase.table("call_records").upsert(
                record_payload,
                on_conflict="room_name"
            ).execute()
            logger.info(f"Successfully recorded call in Supabase for room: {room_name}")
            return JSONResponse({
                "status": "success",
                "event": "room_finished",
                "room_name": room_name,
                "duration_seconds": duration_seconds
            }, status_code=200)
        except Exception as db_exc:
            logger.error(f"Error persisting call record to Supabase: {db_exc}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Database persistence error: {str(db_exc)}"
            )

    # ---------------------------------------------------------
    # Event 2: egress_ended -> Update Recording Link
    # ---------------------------------------------------------
    elif event_type == "egress_ended":
        egress_info = getattr(event, "egress_info", None)
        if not egress_info:
            logger.warning("egress_ended event received without egress_info.")
            return JSONResponse({"status": "ignored", "reason": "No egress_info"}, status_code=200)

        room_name = getattr(egress_info, "room_name", None)
        recording_url = extract_recording_url(egress_info)

        logger.info(f"Processing egress_ended for room '{room_name}'. Recording URL: {recording_url}")

        if not room_name:
            logger.warning("egress_ended event missing room_name; cannot link to call record.")
            return JSONResponse({"status": "ignored", "reason": "Missing room_name"}, status_code=200)

        if not recording_url:
            logger.warning(f"No recording location found in egress_info for room {room_name}")
            recording_url = "NO_RECORDING_URL_FOUND"

        try:
            # Update existing row with recording_url
            update_res = supabase.table("call_records").update({
                "recording_url": recording_url
            }).eq("room_name", room_name).execute()

            # Handle race condition: if egress_ended arrived before room_finished
            if not update_res.data:
                logger.info(f"Room {room_name} not yet present in call_records. Pre-creating placeholder.")
                now_iso = datetime.now(timezone.utc).isoformat()
                supabase.table("call_records").insert({
                    "room_name": room_name,
                    "caller_phone": "Pending",
                    "start_time": now_iso,
                    "end_time": now_iso,
                    "duration_seconds": 0,
                    "recording_url": recording_url,
                }).execute()

            logger.info(f"Successfully linked recording URL to room: {room_name}")
            return JSONResponse({
                "status": "success",
                "event": "egress_ended",
                "room_name": room_name,
                "recording_url": recording_url
            }, status_code=200)

        except Exception as db_exc:
            logger.error(f"Error updating recording URL in Supabase: {db_exc}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Database update error: {str(db_exc)}"
            )

    # Any other events are acknowledged
    return JSONResponse({
        "status": "acknowledged",
        "event": event_type
    }, status_code=200)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("webhook_receiver:app", host="0.0.0.0", port=port, reload=True)
