"""
Callivity Voice AI SaaS Backend - Real-Time Voice Agent
Regional BPO Overflow Voice Assistant for Kerala

Features:
- LiveKit real-time voice orchestration
- Sarvam AI Saaras v4 STT (transcribe mode, flush_signal=True for low-latency barge-in)
- Sarvam AI Bulbul v3 TTS (speaker='shubh' for natural Indic speech)
- Groq Llama 3.1 70B Versatile via OpenAI-compatible LiveKit plugin
- turn_detection="stt" for accurate utterance endpointing
- Malayalam-English (Manglish) code-switching Kerala BPO customer service agent
- Automated warm greeting upon caller connection
- Caller phone number extraction from room/participant metadata & chat context tagging
"""

import json
import logging
import os
import sys
from dotenv import load_dotenv

from livekit import rtc, api
from livekit.agents import (
    AutoSubscribe,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
    llm,
    AgentSession,
    Agent,
)
from livekit.plugins import openai, sarvam

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("callivity-agent")

# BPO Overflow Persona and Guidelines
SYSTEM_PROMPT = """You are a polite, professional, and empathetic customer support executive at Callivity, a regional BPO overflow platform based in Kerala, India.

CORE BEHAVIOR & GUIDELINES:
1. CODE-SWITCHING & MULTILINGUAL CONVERSATION:
   - You are fluent in Malayalam and Indian English.
   - You seamlessly code-switch between Malayalam and English (Manglish) depending on the caller's comfort and phrasing.
   - If the caller speaks Malayalam, converse predominantly in polite Malayalam.
   - If the caller speaks English, converse in clear, professional Indian English.
   - If the caller mixes both languages, code-switch naturally (e.g., "Namaskaram! Callivity customer support-ilekku swagatham. Ente peru Shubh. Ningalkku enganeya sahayeekendathu?").

2. CONVERSATIONAL TONE & VOICE AGENT CONSTRAINTS:
   - Your responses are converted directly to speech using Text-To-Speech.
   - NEVER use markdown formatting, bold/italics (asterisks), bullet points, numbered lists, or emojis.
   - Keep answers conversational, warm, and concise (1 to 3 sentences maximum per turn).
   - Do not overwhelm the caller with long monologues. Allow them to speak and respond interactively.

3. DOMAIN EXPERTISE & OVERFLOW HANDLING:
   - You handle overflow calls for high-demand services: Banking, Telecommunications, Healthcare, and E-commerce delivery tracking.
   - Listen attentively to caller concerns, verify details politely, and provide helpful resolutions or schedule immediate follow-ups.
   - If a caller asks about account status or tickets, collect relevant identifiers (like account number or phone number) and assure them of prompt assistance.
"""


def extract_caller_phone(ctx: JobContext, participant: rtc.Participant | None = None) -> str:
    """
    Extracts the caller's phone number from room metadata or participant identity/metadata.
    Supports LiveKit SIP trunks, PSTN gateways, and web clients.
    """
    # 1. Inspect Room Metadata (often populated by SIP dispatch rules)
    if ctx.room.metadata:
        try:
            meta = json.loads(ctx.room.metadata)
            if isinstance(meta, dict):
                for key in ["caller_phone", "phone_number", "phone", "from", "caller_id", "telephone"]:
                    val = meta.get(key)
                    if val:
                        return str(val).strip()
        except Exception:
            raw_meta = ctx.room.metadata.strip()
            if raw_meta.startswith("+") or raw_meta.isdigit():
                return raw_meta

    # 2. Inspect Participant Metadata
    if participant and participant.metadata:
        try:
            p_meta = json.loads(participant.metadata)
            if isinstance(p_meta, dict):
                for key in ["caller_phone", "phone_number", "phone", "from", "caller_id", "telephone"]:
                    val = p_meta.get(key)
                    if val:
                        return str(val).strip()
        except Exception:
            raw_p_meta = participant.metadata.strip()
            if raw_p_meta.startswith("+") or raw_p_meta.isdigit():
                return raw_p_meta

    # 3. Inspect Participant Identity (LiveKit SIP creates identities like 'sip_+919846012345' or '+919846012345')
    if participant and participant.identity:
        identity = participant.identity.strip()
        if identity.startswith("sip_"):
            return identity.replace("sip_", "")
        if identity.startswith("+") or any(ch.isdigit() for ch in identity):
            return identity

    # 4. Fallback: Check if Room Name contains the phone number
    if ctx.room.name and ("+91" in ctx.room.name or ctx.room.name.startswith("sip_")):
        parts = ctx.room.name.split("_")
        for part in parts:
            if part.startswith("+") or (part.isdigit() and len(part) >= 10):
                return part

    return "+91-UNKNOWN"


async def entrypoint(ctx: JobContext):
    """
    Main entrypoint triggered by LiveKit JobContext when an inbound call room is dispatched.
    """
    logger.info(f"Starting Callivity Voice Agent session for room: {ctx.room.name}")
    
    # Connect to the room and auto-subscribe to incoming audio tracks
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # Wait for the customer/caller participant to join the room
    participant = await ctx.wait_for_participant()
    logger.info(f"Caller connected: Identity={participant.identity}, Name={participant.name}")

    # Extract caller's phone number
    caller_phone = extract_caller_phone(ctx, participant)
    logger.info(f"Identified caller phone number: {caller_phone}")

    # Synchronize caller_phone into room metadata so the room_finished webhook receives it
    try:
        current_meta = {}
        if ctx.room.metadata:
            try:
                current_meta = json.loads(ctx.room.metadata)
                if not isinstance(current_meta, dict):
                    current_meta = {"raw": ctx.room.metadata}
            except Exception:
                current_meta = {"raw": ctx.room.metadata}

        if "caller_phone" not in current_meta:
            current_meta["caller_phone"] = caller_phone
            if hasattr(ctx, "api") and ctx.api:
                await ctx.api.room.update_room_metadata(
                    api.UpdateRoomMetadataRequest(
                        room=ctx.room.name,
                        metadata=json.dumps(current_meta),
                    )
                )
                logger.info(f"Updated room metadata with caller_phone: {caller_phone}")
    except Exception as exc:
        logger.warning(f"Could not update room metadata via LiveKit API: {exc}")

    # STT (Ears): Sarvam AI saaras:v4 in transcribe mode with flush_signal=True for barge-in detection
    sarvam_api_key = os.environ.get("SARVAM_API_KEY")
    if not sarvam_api_key:
        logger.error("SARVAM_API_KEY is not set in environment!")

    stt = sarvam.STT(
        model="saaras:v4",
        mode="transcribe",
        flush_signal=True,
        api_key=sarvam_api_key,
    )

    # LLM (Brain): Groq llama-3.1-70b-versatile via OpenAI-compatible LiveKit plugin
    groq_api_key = os.environ.get("GROQ_API_KEY")
    if not groq_api_key:
        logger.error("GROQ_API_KEY is not set in environment!")

    llm_instance = openai.LLM(
        base_url="https://api.groq.com/openai/v1",
        api_key=groq_api_key,
        model="llama-3.1-70b-versatile",
    )

    # TTS (Voice): Sarvam AI bulbul:v3 with speaker 'shubh'
    tts = sarvam.TTS(
        model="bulbul:v3",
        speaker="shubh",
        api_key=sarvam_api_key,
    )

    # Instantiate the unified AgentSession with turn_detection="stt"
    session = AgentSession(
        stt=stt,
        llm=llm_instance,
        tts=tts,
        turn_detection="stt",
    )

    # Create the agent with Malayalam/English Kerala BPO instructions
    agent = Agent(
        instructions=SYSTEM_PROMPT,
    )

    # Start the session within the connected room
    await session.start(room=ctx.room, agent=agent)

    # Metadata Tagging: Append caller phone number into chat context so LLM & webhooks capture it
    metadata_context_message = (
        f"[SYSTEM CONTEXT - CALLIVITY METADATA]\n"
        f"- Caller Phone Number: {caller_phone}\n"
        f"- LiveKit Room: {ctx.room.name}\n"
        f"- Tenant: Callivity Regional BPO Overflow\n"
        f"- Routing Language: Malayalam & English (Code-switch enabled)\n"
    )

    if hasattr(session, "chat_context") and session.chat_context is not None:
        session.chat_context.append(role="system", text=metadata_context_message)
    elif hasattr(agent, "chat_ctx") and agent.chat_ctx is not None:
        agent.chat_ctx.append(role="system", text=metadata_context_message)
    logger.info("Caller phone number successfully appended to chat context.")

    # Automatically trigger a warm greeting upon caller connection
    logger.info("Triggering initial warm greeting...")
    await session.generate_reply(
        instructions="Greet the caller warmly in Malayalam and English, introducing yourself as Shubh from Callivity customer support, and ask how you can assist them today."
    )


if __name__ == "__main__":
    # Run the worker via LiveKit CLI interface
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
        )
    )
