# Callivity Voice AI SaaS Backend

Production-ready regional BPO overflow voice intelligence platform built with **LiveKit Real-Time Voice Agents**, **Sarvam AI**, **Groq Llama 3.1**, and **FastAPI** with **Supabase PostgreSQL** for automated billable minute logging and audio recording tracking.

---

## 🏛️ System Architecture

```
[Inbound Caller (PSTN/SIP)]
             │
             ▼
     LiveKit Cloud SIP
             │ (WebRTC)
             ▼
   ┌────────────────────────────────────────────────────────┐
   │                  agent.py (Worker)                     │
   │  - STT: Sarvam Saaras:v4 (mode="transcribe")           │
   │  - Turn Detection: STT-driven (flush_signal=True)      │
   │  - LLM: Groq Llama-3.1-70b-versatile (OpenAI plugin)   │
   │  - TTS: Sarvam Bulbul:v3 (speaker="shubh")             │
   │  - Persona: Kerala BPO (Malayalam-English Code Switch) │
   └────────────────────────────────────────────────────────┘
             │
             ├── Room Finished / Egress Ended Events (Signed Webhooks)
             ▼
   ┌────────────────────────────────────────────────────────┐
   │            webhook_receiver.py (FastAPI)               │
   │  - LiveKit WebhookReceiver HMAC Verification           │
   │  - Duration Calculation: (end_time - start_time)       │
   │  - Caller Phone Extraction                             │
   │  - Egress S3/GCP Recording URL Linkage                 │
   └────────────────────────────────────────────────────────┘
             │
             ▼
   ┌────────────────────────────────────────────────────────┐
   │              Supabase PostgreSQL Database              │
   │  - Table: call_records                                 │
   │  - Stored Generated Column: billable_minutes           │
   │  - Indexes on room_name, caller_phone, start_time      │
   └────────────────────────────────────────────────────────┘
```

---

## 📁 Repository Structure

| File | Purpose |
| :--- | :--- |
| [`agent.py`](./agent.py) | LiveKit voice agent worker with Sarvam STT/TTS and Groq LLM |
| [`webhook_receiver.py`](./webhook_receiver.py) | FastAPI webhook receiver logging call records to Supabase |
| [`schema.sql`](./schema.sql) | Supabase PostgreSQL schema with `call_records` table and RLS |
| [`requirements.txt`](./requirements.txt) | Pinned Python production dependencies |
| [`livekit.toml`](./livekit.toml) | LiveKit Cloud agent deployment manifest |
| [`.env.example`](./.env.example) | Environment variable template |

---

## 🚀 Setup & Local Development

### 1. Install Dependencies

```bash
python -m venv venv
# Windows:
.\venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure Environment Variables

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

```env
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your_livekit_api_key
LIVEKIT_API_SECRET=your_livekit_api_secret

SARVAM_API_KEY=your_sarvam_api_key
GROQ_API_KEY=gsk_your_groq_api_key

SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your_supabase_service_role_key
PORT=8000
```

### 3. Initialize Supabase Database

1. Open your [Supabase Dashboard](https://supabase.com/dashboard).
2. Go to **SQL Editor** -> **New Query**.
3. Paste the contents of [`schema.sql`](./schema.sql) and run the query.

### 4. Run the Voice Agent Locally

To run the agent in development/console mode:

```bash
python agent.py dev
```

Or connect the worker to your LiveKit Cloud room:

```bash
python agent.py start
```

### 5. Run the Webhook Receiver

```bash
python webhook_receiver.py
# or with uvicorn directly:
uvicorn webhook_receiver:app --host 0.0.0.0 --port 8000 --reload
```

---

## ☁️ Deploying to LiveKit Cloud

To deploy your agent worker directly to LiveKit Cloud:

### 1. Authenticate with LiveKit Cloud

```bash
lk cloud auth
```
*(This opens a browser window to authenticate with your LiveKit Cloud credentials and link your project.)*

### 2. Register & Deploy Agent

For the initial deployment:
```bash
lk agent create
```

For subsequent updates / continuous deployment:
```bash
lk agent deploy
```

---

## 🔗 Configuring the Webhook in LiveKit Cloud

1. Go to the [LiveKit Cloud Console](https://cloud.livekit.io) -> **Settings** -> **Webhooks**.
2. Click **Add Webhook**.
3. Set the Webhook URL to: `https://your-fastapi-domain.com/livekit/webhook`.
4. Select the following events:
   - `room_finished`
   - `egress_ended`
5. Save changes. LiveKit will automatically sign all requests with your project's `LIVEKIT_API_SECRET` in the `Authorization` header.
