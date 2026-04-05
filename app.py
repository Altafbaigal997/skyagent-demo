import streamlit as st
import time
import re
import random
import hashlib
import json
import os
from datetime import datetime, timedelta

try:
    from groq import Groq
    GROQ_AVAILABLE = True
except ImportError:
    GROQ_AVAILABLE = False

# ════════════════════════════════════════════════════════════════════════════════
# PAGE CONFIG & STATE INITIALIZATION
# ════════════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="SkyAgent AI · Your AI Travel Agent",
    page_icon="✈️",
    layout="wide",
    initial_sidebar_state="expanded",
)

if "messages" not in st.session_state:
    st.session_state.messages = []
    st.session_state.stage = "greeting"
    st.session_state.trip = {}
    st.session_state.flights = []
    st.session_state.selected_flight = None
    st.session_state.booking_ref = None
    st.session_state.pnr = None
    st.session_state.hotels = []
    st.session_state.selected_hotel = None
    st.session_state.hotel_booking_ref = None
    st.session_state.hotel_confirmation_id = None
    st.session_state.greeted = False

# ════════════════════════════════════════════════════════════════════════════════
# LLM ENGINE (Groq - Llama 3.3 70B)
# ════════════════════════════════════════════════════════════════════════════════

def get_groq_client():
    """Get Groq client from secrets, env, or sidebar input."""
    api_key = None
    # Try Streamlit secrets first (for Streamlit Cloud deployment)
    try:
        api_key = st.secrets.get("GROQ_API_KEY", None)
    except Exception:
        pass
    # Try environment variable
    if not api_key:
        api_key = os.environ.get("GROQ_API_KEY", "")
    # Try session state (from sidebar input)
    if not api_key and "groq_api_key" in st.session_state:
        api_key = st.session_state.groq_api_key
    if api_key and GROQ_AVAILABLE:
        return Groq(api_key=api_key)
    return None


def match_city_name(name):
    """Match an LLM-extracted city name to CITY_DB. Returns (code, display) or (None, None)."""
    if not name:
        return None, None
    name_lower = name.lower().strip()
    # Exact key match
    if name_lower in CITY_DB:
        return CITY_DB[name_lower]
    # Match against display names
    for key, (code, display) in CITY_DB.items():
        if display.lower() == name_lower:
            return code, display
    # Partial match (e.g., "new york city" → "new york")
    for key, (code, display) in sorted(CITY_DB.items(), key=lambda x: -len(x[0])):
        if key in name_lower or name_lower in key:
            return code, display
    return None, None


def llm_parse(user_input, stage):
    """Use Groq LLM to parse user intent and extract entities, with conversation context."""
    client = get_groq_client()
    if not client:
        return None

    city_list = ", ".join(sorted(set(display for _, (_, display) in CITY_DB.items())))
    today = datetime.now().strftime("%A, %B %d, %Y")

    # ── Build booking context so LLM knows current state ──
    booking_context = "CURRENT BOOKING STATE:\n"
    if st.session_state.selected_flight:
        f = st.session_state.selected_flight
        booking_context += (
            f"- Flight BOOKED: {f['airline']} {f['flight_num']}, "
            f"{f['origin_name']} → {f['dest_name']}, "
            f"{f['date']}, PNR: {st.session_state.pnr or 'N/A'}, "
            f"${f['price']}.00\n"
        )
    else:
        booking_context += "- No flight booked yet\n"

    if st.session_state.selected_hotel:
        h = st.session_state.selected_hotel
        room_cost = h["price_per_night"] * h["nights"]
        taxes = int(room_cost * 0.13)
        service_fee = int(room_cost * 0.05)
        total = room_cost + taxes + service_fee
        booking_context += (
            f"- Hotel BOOKED: {h['name']} ({h['stars']}★), "
            f"{h['area']}, {h['room_type']}, {h['nights']} nights, "
            f"Confirmation: {st.session_state.hotel_confirmation_id or 'N/A'}, "
            f"${total}.00 {h['currency']}\n"
        )
    else:
        booking_context += "- No hotel booked yet\n"

    # ── Build recent conversation history (last 6 messages) ──
    recent_history = ""
    msgs = st.session_state.messages[-6:] if st.session_state.messages else []
    if msgs:
        recent_history = "RECENT CONVERSATION:\n"
        for msg in msgs:
            role = "User" if msg["role"] == "user" else "Agent"
            content = msg.get("content", "")
            if content:
                # Truncate long messages
                if len(content) > 200:
                    content = content[:200] + "..."
                recent_history += f"  {role}: {content}\n"

    system_prompt = f"""You are the intent parser for SkyAgent AI, a flight and hotel booking assistant.

Today's date is: {today}
Current conversation stage: {stage}

{booking_context}
{recent_history}
SUPPORTED CITIES (only these are available for booking):
{city_list}

Your job: Extract the user's intent and entities from their LATEST message, using the conversation history and booking state for context. Return ONLY valid JSON.

JSON schema:
{{
  "intent": "search_flight" | "select_option" | "confirm" | "cancel" | "ask_hotel" | "ask_itinerary" | "skip" | "check_status" | "new_search" | "cancel_booking" | "cancel_hotel" | "general",
  "origin": "city name as said by user" or null,
  "destination": "city name as said by user" or null,
  "origin_supported": true/false (is the origin city in the SUPPORTED CITIES list above?),
  "destination_supported": true/false (is the destination city in the SUPPORTED CITIES list above?),
  "date": "resolved date like Friday, April 11, 2026" or null,
  "date_valid": true/false (is the date valid and in the future?),
  "date_error": "past_date" | "no_date" | "vague_date" | null,
  "budget": number or null,
  "selection": 1 or 2 or 3 or null,
  "is_yes": true/false,
  "is_no": true/false,
  "agent_reply": "natural travel agent response" or null
}}

RULES:
1. DATES — this is critical for accuracy:
   - Resolve ALL relative dates to actual calendar dates using today's date. "next Friday" → calculate the actual date. "tomorrow" → calculate. "June 15" → resolve to the NEXT occurrence (2026 or 2027). Always return format: "DayOfWeek, Month Day, Year" (e.g., "Friday, April 18, 2026").
   - If user says a PAST date (e.g., "April 21, 2025" when today is in 2026), set date_valid=false and date_error="past_date".
   - If user says a vague date like "next weekend", resolve to the specific Saturday date. "This weekend" → the coming Saturday. "Next weekend" → the Saturday after this weekend.
   - If user gives a date without a year (e.g., "March 15"), assume the NEXT future occurrence. If March 15 has passed in 2026, use March 15, 2027.
   - If NO date is mentioned at all, set date to null and date_error="no_date".
   - If the date is ambiguous or unclear (e.g., "sometime in summer"), set date to null and date_error="vague_date".
2. CITIES: Check if the mentioned cities are in the SUPPORTED CITIES list. Set origin_supported/destination_supported accordingly. Be case-insensitive.
3. SELECTION: Extract option numbers from phrases like "book option 2", "the first one", "hotel 3", "#2", "second" → 2.
4. INTENT CLASSIFICATION — this is critical. Use the CONVERSATION HISTORY and BOOKING STATE to understand context:
   - "search_flight": user wants to find or search for flights
   - "select_option": user is choosing an option (flight 1, hotel 2, etc.)
   - "confirm": user is saying YES to proceed with a payment or action. Also use for "yes please cancel" when agent just asked "Shall I proceed with cancellation?"
   - "cancel": user is saying NO / declining the current action being offered (e.g., declining payment, declining hotel search)
   - "cancel_booking": user wants to CANCEL AN EXISTING FLIGHT BOOKING. Look at booking state — if a flight is booked and user says "cancel it", "cancel my booking", "cancel the flight", "I don't want this flight", this is cancel_booking. Also if user says "cancel it" and the conversation was about the flight, this is cancel_booking.
   - "cancel_hotel": user wants to CANCEL AN EXISTING HOTEL RESERVATION. If a hotel is booked and user says "cancel hotel", "cancel my room", "cancel the hotel reservation", this is cancel_hotel.
   - "ask_hotel": user is asking about hotels
   - "ask_itinerary": user wants an itinerary
   - "skip": user wants to skip the current step
   - "check_status": user wants to check booking status
   - "new_search": user EXPLICITLY wants to start a completely new search/trip. Only use this if user says things like "new search", "start over", "plan a different trip". Do NOT use this for cancellation requests.
   - "general": anything else — chitchat, questions about the service, etc.
5. CONTEXT RESOLUTION — this is very important:
   - Use conversation history to resolve pronouns. "cancel it" → look at what was last discussed. If the agent just showed a flight booking, "it" = the flight. If the agent just showed a hotel, "it" = the hotel.
   - "yes, cancel and also cancel the flight" → TWO intents. Pick the most actionable one: cancel_booking (since the hotel cancel was already being processed).
   - "did you cancel it?" after a cancellation discussion → check_status intent.
   - If the user says "yes" after the agent asked "Shall I proceed with cancellation?" → intent is "confirm", is_yes=true.
6. AGENT_REPLY: Fill this when intent is "general", when a city is not supported, or when you need to confirm/acknowledge a cancellation confirmation. For unsupported cities, write a friendly message listing alternatives. For general questions, respond as a helpful travel agent. Keep replies concise (2-3 sentences max).
7. If the user mentions a city for origin but not destination (or vice versa), only fill what's mentioned. Don't guess.
8. is_yes/is_no: Set these for simple affirmative/negative responses. Do NOT set is_no=true when intent is "cancel_booking" or "cancel_hotel" — those are booking actions, not negative responses.
9. If user says "yes", "sure", "go ahead" → is_yes=true. If "no", "nah", "skip" → is_no=true."""

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input}
            ],
            temperature=0.1,
            max_tokens=500,
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content)
        return result
    except Exception as e:
        st.write(f"")  # silent fail
        return None


# ════════════════════════════════════════════════════════════════════════════════
# CUSTOM CSS
# ════════════════════════════════════════════════════════════════════════════════

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,500;0,9..40,700&family=Space+Mono:wght@400;700&display=swap');

/* ── Global ── */
section[data-testid="stMain"] { background: #f5f7fb !important; }
div[data-testid="stChatMessage"] { font-family: 'DM Sans', sans-serif; }
[data-testid="stAppViewContainer"] { background: #f5f7fb !important; }
[data-testid="stHeader"] { background: transparent !important; }
[data-testid="stChatInput"] textarea { font-family: 'DM Sans', sans-serif !important; }
p, span, li, div { color: inherit; }

/* ── App Header Banner ── */
.app-header {
    text-align: center;
    padding: 30px 20px 24px 20px;
    margin: -10px -1rem 20px -1rem;
    background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 50%, #0f172a 100%);
    border-radius: 0 0 24px 24px;
    box-shadow: 0 4px 20px rgba(15,23,42,0.15);
}
.app-header-icon { font-size: 40px; margin-bottom: 6px; }
.app-header-title {
    font-family: 'DM Sans', sans-serif;
    font-size: 28px; font-weight: 700;
    color: #ffffff !important;
    letter-spacing: 0.5px;
    margin-bottom: 4px;
}
.app-header-sub {
    font-family: 'DM Sans', sans-serif;
    font-size: 13px; color: #94a3b8 !important;
    letter-spacing: 0.3px;
}

/* ── Sidebar ── */
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0a1628 0%, #132042 100%);
    color: #c8d6e5;
}
section[data-testid="stSidebar"] h1,
section[data-testid="stSidebar"] h2,
section[data-testid="stSidebar"] h3 { color: #ffffff; }
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] li,
section[data-testid="stSidebar"] span { color: #a4b4c8; }
section[data-testid="stSidebar"] hr { border-color: rgba(255,255,255,0.08); }

/* ── Flight Card ── */
.flight-card {
    background: #ffffff;
    border-radius: 14px;
    padding: 22px 24px;
    margin: 12px 0;
    box-shadow: 0 1px 4px rgba(10,22,40,0.06), 0 4px 14px rgba(10,22,40,0.04);
    border-left: 5px solid #2563eb;
    transition: box-shadow 0.2s;
    font-family: 'DM Sans', sans-serif;
}
.flight-card:hover { box-shadow: 0 6px 24px rgba(37,99,235,0.12); }

.option-badge {
    background: #2563eb; color: #fff;
    padding: 3px 14px; border-radius: 20px;
    font-size: 11px; font-weight: 700;
    letter-spacing: 0.5px; text-transform: uppercase;
    display: inline-block; margin-bottom: 10px;
}
.best-badge {
    background: linear-gradient(135deg, #f59e0b 0%, #ef4444 100%);
    color: #fff; padding: 3px 12px; border-radius: 20px;
    font-size: 10px; font-weight: 700;
    letter-spacing: 0.5px; text-transform: uppercase;
    display: inline-block; margin-left: 8px;
}
.airline-row {
    display: flex; align-items: center; gap: 10px;
    margin-bottom: 14px;
}
.airline-emoji { font-size: 22px; }
.airline-name { font-weight: 700; font-size: 15px; color: #1e293b; }
.airline-code { color: #94a3b8; font-size: 12px; font-family: 'Space Mono', monospace; }

.route-row {
    display: flex; align-items: center; gap: 0; margin: 12px 0;
}
.route-point { text-align: center; flex: 0 0 auto; }
.route-time { font-size: 22px; font-weight: 700; color: #0f172a; font-family: 'Space Mono', monospace; }
.route-code { font-size: 13px; color: #64748b; font-weight: 500; }
.route-line-wrapper { flex: 1; display: flex; flex-direction: column; align-items: center; padding: 0 16px; }
.route-line {
    width: 100%; height: 2px;
    background: linear-gradient(90deg, #2563eb 0%, #93c5fd 50%, #2563eb 100%);
    position: relative; margin: 4px 0;
}
.route-line::before {
    content: '✈'; position: absolute; top: -11px; left: 50%;
    transform: translateX(-50%); font-size: 16px;
}
.route-duration { font-size: 11px; color: #94a3b8; font-weight: 500; }
.route-stops { font-size: 11px; color: #f59e0b; font-weight: 600; }

.price-tag {
    font-size: 26px; font-weight: 700; color: #2563eb;
    font-family: 'Space Mono', monospace;
    margin-top: 10px;
}
.price-note { font-size: 11px; color: #94a3b8; }

/* ── Payment Card ── */
.payment-card {
    background: linear-gradient(135deg, #0f172a 0%, #1e293b 50%, #0f172a 100%);
    border-radius: 18px; padding: 28px 30px;
    color: #e2e8f0; margin: 14px 0;
    font-family: 'DM Sans', sans-serif;
    box-shadow: 0 8px 32px rgba(0,0,0,0.25);
}
.payment-card h3 {
    color: #38bdf8; margin: 0 0 6px 0;
    font-size: 18px; font-weight: 700;
}
.payment-subtitle { color: #64748b; font-size: 12px; margin-bottom: 18px; }
.pay-line {
    display: flex; justify-content: space-between;
    padding: 7px 0; border-bottom: 1px solid rgba(255,255,255,0.06);
    font-size: 14px;
}
.pay-line-label { color: #94a3b8; }
.pay-line-value { color: #e2e8f0; font-weight: 500; font-family: 'Space Mono', monospace; }
.pay-total {
    display: flex; justify-content: space-between;
    padding: 14px 0 0 0; margin-top: 10px;
    border-top: 2px solid #38bdf8;
    font-size: 20px; font-weight: 700;
}
.pay-total-label { color: #f8fafc; }
.pay-total-value { color: #38bdf8; font-family: 'Space Mono', monospace; }
.pay-card-info {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 10px; padding: 14px;
    margin-top: 18px; display: flex;
    align-items: center; gap: 14px;
}
.pay-card-icon { font-size: 28px; }
.pay-card-label { font-size: 11px; color: #64748b; }
.pay-card-number { font-size: 15px; color: #f1f5f9; font-family: 'Space Mono', monospace; font-weight: 600; }

/* ── Confirmation Card ── */
.confirm-card {
    background: linear-gradient(135deg, #065f46 0%, #059669 100%);
    border-radius: 18px; padding: 30px; color: #fff;
    margin: 14px 0; text-align: center;
    box-shadow: 0 8px 32px rgba(5,150,105,0.25);
    font-family: 'DM Sans', sans-serif;
}
.confirm-icon { font-size: 48px; margin-bottom: 10px; }
.confirm-title { font-size: 22px; font-weight: 700; margin-bottom: 4px; }
.confirm-sub { color: #a7f3d0; font-size: 14px; margin-bottom: 20px; }
.confirm-details {
    background: rgba(255,255,255,0.12); border-radius: 10px;
    padding: 16px; text-align: left;
    display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
}
.confirm-item-label { font-size: 11px; color: #a7f3d0; text-transform: uppercase; letter-spacing: 0.5px; }
.confirm-item-value { font-size: 14px; font-weight: 600; }

/* ── Itinerary ── */
.itin-day {
    background: #ffffff; border-radius: 14px;
    padding: 22px 24px; margin: 12px 0;
    box-shadow: 0 1px 4px rgba(10,22,40,0.06), 0 4px 14px rgba(10,22,40,0.04);
    font-family: 'DM Sans', sans-serif;
}
.itin-day-header {
    display: flex; align-items: center; gap: 12px;
    margin-bottom: 14px; padding-bottom: 10px;
    border-bottom: 2px solid #eff6ff;
}
.itin-day-num {
    background: #2563eb; color: #fff;
    width: 32px; height: 32px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 14px;
}
.itin-day-title { font-weight: 700; font-size: 17px; color: #0f172a; }
.itin-item {
    display: flex; gap: 14px; padding: 10px 0;
    border-bottom: 1px solid #f1f5f9;
}
.itin-item:last-child { border-bottom: none; }
.itin-time {
    color: #2563eb; font-weight: 600; font-size: 13px;
    font-family: 'Space Mono', monospace;
    min-width: 76px; padding-top: 2px;
}
.itin-emoji { font-size: 18px; padding-top: 1px; }
.itin-desc { color: #334155; font-size: 14px; line-height: 1.5; }

/* ── Hotel Card ── */
.hotel-card {
    background: #ffffff;
    border-radius: 14px;
    padding: 22px 24px;
    margin: 12px 0;
    box-shadow: 0 1px 4px rgba(10,22,40,0.06), 0 4px 14px rgba(10,22,40,0.04);
    border-left: 5px solid #7c3aed;
    transition: box-shadow 0.2s;
    font-family: 'DM Sans', sans-serif;
}
.hotel-card:hover { box-shadow: 0 6px 24px rgba(124,58,237,0.12); }

.hotel-option-badge {
    background: #7c3aed; color: #fff;
    padding: 3px 14px; border-radius: 20px;
    font-size: 11px; font-weight: 700;
    letter-spacing: 0.5px; text-transform: uppercase;
    display: inline-block; margin-bottom: 10px;
}
.hotel-top-badge {
    background: linear-gradient(135deg, #f59e0b 0%, #ef4444 100%);
    color: #fff; padding: 3px 12px; border-radius: 20px;
    font-size: 10px; font-weight: 700;
    letter-spacing: 0.5px; text-transform: uppercase;
    display: inline-block; margin-left: 8px;
}
.hotel-name-row {
    display: flex; align-items: center; gap: 10px;
    margin-bottom: 6px;
}
.hotel-name { font-weight: 700; font-size: 17px; color: #1e293b; }
.hotel-stars { color: #f59e0b; font-size: 14px; letter-spacing: 1px; }
.hotel-address { color: #64748b; font-size: 13px; margin-bottom: 12px; }
.hotel-details-row {
    display: flex; gap: 18px; flex-wrap: wrap;
    margin-bottom: 12px;
}
.hotel-detail-chip {
    background: #f5f3ff; color: #6d28d9;
    padding: 4px 12px; border-radius: 8px;
    font-size: 12px; font-weight: 500;
}
.hotel-amenities {
    color: #64748b; font-size: 12px; margin-bottom: 14px;
    line-height: 1.6;
}
.hotel-bottom-row {
    display: flex; justify-content: space-between;
    align-items: flex-end; margin-top: 8px;
    padding-top: 12px; border-top: 1px solid #f1f5f9;
}
.hotel-rating {
    display: flex; align-items: center; gap: 8px;
}
.hotel-rating-score {
    background: #7c3aed; color: #fff;
    padding: 4px 10px; border-radius: 8px;
    font-weight: 700; font-size: 14px;
    font-family: 'Space Mono', monospace;
}
.hotel-rating-label { color: #64748b; font-size: 12px; }
.hotel-rating-reviews { color: #94a3b8; font-size: 11px; }
.hotel-price-block { text-align: right; }
.hotel-price {
    font-size: 24px; font-weight: 700; color: #7c3aed;
    font-family: 'Space Mono', monospace;
}
.hotel-price-note { font-size: 11px; color: #94a3b8; }
.hotel-cancel-policy { font-size: 11px; color: #059669; font-weight: 500; margin-top: 3px; }
.hotel-cancel-policy.non-refundable { color: #dc2626; }

/* ── Hotel Payment Card ── */
.hotel-payment-card {
    background: linear-gradient(135deg, #1e1040 0%, #2d1b69 50%, #1e1040 100%);
    border-radius: 18px; padding: 28px 30px;
    color: #e2e8f0; margin: 14px 0;
    font-family: 'DM Sans', sans-serif;
    box-shadow: 0 8px 32px rgba(45,27,105,0.3);
}
.hotel-payment-card h3 {
    color: #a78bfa; margin: 0 0 6px 0;
    font-size: 18px; font-weight: 700;
}

/* ── Hotel Confirmation ── */
.hotel-confirm-card {
    background: linear-gradient(135deg, #4c1d95 0%, #7c3aed 100%);
    border-radius: 18px; padding: 30px; color: #fff;
    margin: 14px 0; text-align: center;
    box-shadow: 0 8px 32px rgba(124,58,237,0.3);
    font-family: 'DM Sans', sans-serif;
}

/* ── Misc ── */
.agent-thinking {
    color: #64748b; font-style: italic; font-size: 14px;
    padding: 4px 0;
}
.suggestion-chip {
    display: inline-block; background: #eff6ff;
    color: #2563eb; padding: 6px 16px;
    border-radius: 20px; font-size: 13px;
    font-weight: 500; margin: 4px 4px 4px 0;
    border: 1px solid #bfdbfe;
}
</style>
""", unsafe_allow_html=True)

# ════════════════════════════════════════════════════════════════════════════════
# DATA: CITIES, AIRLINES, ITINERARIES
# ════════════════════════════════════════════════════════════════════════════════

CITY_DB = {
    "lahore": ("LHE", "Lahore"), "karachi": ("KHI", "Karachi"),
    "islamabad": ("ISB", "Islamabad"), "peshawar": ("PEW", "Peshawar"),
    "london": ("LHR", "London"), "new york": ("JFK", "New York"),
    "dubai": ("DXB", "Dubai"), "paris": ("CDG", "Paris"),
    "tokyo": ("NRT", "Tokyo"), "istanbul": ("IST", "Istanbul"),
    "singapore": ("SIN", "Singapore"), "bangkok": ("BKK", "Bangkok"),
    "toronto": ("YYZ", "Toronto"), "sydney": ("SYD", "Sydney"),
    "jeddah": ("JED", "Jeddah"), "riyadh": ("RUH", "Riyadh"),
    "doha": ("DOH", "Doha"), "mumbai": ("BOM", "Mumbai"),
    "delhi": ("DEL", "Delhi"), "kuala lumpur": ("KUL", "Kuala Lumpur"),
    "los angeles": ("LAX", "Los Angeles"), "san francisco": ("SFO", "San Francisco"),
    "chicago": ("ORD", "Chicago"), "barcelona": ("BCN", "Barcelona"),
    "rome": ("FCO", "Rome"), "amsterdam": ("AMS", "Amsterdam"),
    "frankfurt": ("FRA", "Frankfurt"), "beijing": ("PEK", "Beijing"),
    "hong kong": ("HKG", "Hong Kong"), "cairo": ("CAI", "Cairo"),
    "abu dhabi": ("AUH", "Abu Dhabi"), "muscat": ("MCT", "Muscat"),
    "colombo": ("CMB", "Colombo"), "dhaka": ("DAC", "Dhaka"),
    "male": ("MLE", "Malé"), "bali": ("DPS", "Bali"),
    "seattle": ("SEA", "Seattle"), "boston": ("BOS", "Boston"),
    "washington": ("IAD", "Washington D.C."),
    "milan": ("MXP", "Milan"), "madrid": ("MAD", "Madrid"),
    "berlin": ("BER", "Berlin"), "vienna": ("VIE", "Vienna"),
    "zurich": ("ZRH", "Zurich"), "mumbai": ("BOM", "Mumbai"),
}

AIRLINE_POOL = [
    {"name": "Emirates", "code": "EK", "hub": "Dubai (DXB)", "tier": "premium"},
    {"name": "Qatar Airways", "code": "QR", "hub": "Doha (DOH)", "tier": "premium"},
    {"name": "Turkish Airlines", "code": "TK", "hub": "Istanbul (IST)", "tier": "premium"},
    {"name": "Etihad Airways", "code": "EY", "hub": "Abu Dhabi (AUH)", "tier": "premium"},
    {"name": "British Airways", "code": "BA", "hub": "London (LHR)", "tier": "premium"},
    {"name": "PIA", "code": "PK", "hub": "Direct", "tier": "standard"},
    {"name": "Singapore Airlines", "code": "SQ", "hub": "Singapore (SIN)", "tier": "premium"},
    {"name": "Lufthansa", "code": "LH", "hub": "Frankfurt (FRA)", "tier": "premium"},
    {"name": "Saudi Airlines", "code": "SV", "hub": "Jeddah (JED)", "tier": "standard"},
    {"name": "AirBlue", "code": "PA", "hub": "Direct", "tier": "budget"},
    {"name": "Thai Airways", "code": "TG", "hub": "Bangkok (BKK)", "tier": "standard"},
    {"name": "Cathay Pacific", "code": "CX", "hub": "Hong Kong (HKG)", "tier": "premium"},
    {"name": "KLM", "code": "KL", "hub": "Amsterdam (AMS)", "tier": "premium"},
    {"name": "Air France", "code": "AF", "hub": "Paris (CDG)", "tier": "premium"},
    {"name": "flydubai", "code": "FZ", "hub": "Dubai (DXB)", "tier": "budget"},
    {"name": "SriLankan Airlines", "code": "UL", "hub": "Colombo (CMB)", "tier": "standard"},
]

ITINERARY_DB = {
    "london": {
        "title": "London",
        "days": [
            {
                "title": "Royal London & Historic Landmarks",
                "items": [
                    ("09:00 AM", "🏛️", "British Museum — Explore the Rosetta Stone, Egyptian mummies & Parthenon galleries (free entry)"),
                    ("12:30 PM", "🍽️", "Lunch at Borough Market — Artisan food stalls, fresh seafood, raclette, and pastries"),
                    ("02:30 PM", "🏰", "Tower of London — See the Crown Jewels and walk the medieval fortress walls"),
                    ("04:30 PM", "🌉", "Walk across Tower Bridge — Photo stop with iconic Thames views"),
                    ("06:00 PM", "🍸", "Drinks at Sky Garden — Free rooftop garden with 360° London views"),
                    ("08:00 PM", "🎭", "West End Show — Catch a world-class musical (Les Misérables or Wicked)"),
                ],
            },
            {
                "title": "Westminster, Parks & South Bank",
                "items": [
                    ("09:30 AM", "🏫", "Westminster Abbey — Centuries of royal history and stunning Gothic architecture"),
                    ("11:00 AM", "🏛️", "Houses of Parliament & Big Ben — Walk along the Thames for the classic view"),
                    ("12:00 PM", "🌳", "St. James's Park — Stroll through and watch the Buckingham Palace guard change"),
                    ("01:30 PM", "🍽️", "Lunch in Covent Garden — Choose from dozens of restaurants and street performers"),
                    ("03:30 PM", "🎨", "Tate Modern — World-class modern art in a converted power station (free entry)"),
                    ("06:00 PM", "🛍️", "South Bank walk — Street food, bookstalls, and live music along the river"),
                    ("08:00 PM", "🍕", "Dinner at Flat Iron Square — Trendy food hall with global cuisines"),
                ],
            },
            {
                "title": "Notting Hill, Markets & Departure",
                "items": [
                    ("09:00 AM", "🏘️", "Notting Hill — Pastel-coloured houses and charming local cafés"),
                    ("10:30 AM", "🛍️", "Portobello Road Market — Vintage finds, antiques, and street food"),
                    ("12:30 PM", "🍽️", "Lunch at The Churchill Arms — Iconic flower-covered pub with Thai food"),
                    ("02:00 PM", "🌿", "Hyde Park — Relax by the Serpentine or visit Kensington Palace"),
                    ("04:00 PM", "🧳", "Head to hotel for luggage & airport transfer"),
                ],
            },
        ],
    },
    "dubai": {
        "title": "Dubai",
        "days": [
            {
                "title": "Downtown Dubai & Modern Marvels",
                "items": [
                    ("09:00 AM", "🏙️", "Burj Khalifa — Visit the observation deck on the 148th floor for jaw-dropping views"),
                    ("11:30 AM", "🛍️", "Dubai Mall — Explore the world's largest mall, aquarium, and ice rink"),
                    ("01:00 PM", "🍽️", "Lunch at Time Out Market — Curated food hall with top Dubai chefs"),
                    ("03:00 PM", "⛲", "Dubai Fountain Show — Watch the choreographed water & light spectacle"),
                    ("05:00 PM", "🏖️", "JBR Beach — Sunset walk along Jumeirah Beach Residence"),
                    ("07:30 PM", "🍽️", "Dinner at Pierchic — Overwater restaurant with fresh seafood and Arabian Gulf views"),
                ],
            },
            {
                "title": "Old Dubai, Culture & Desert Safari",
                "items": [
                    ("09:00 AM", "⛵", "Abra ride across Dubai Creek — Traditional wooden boat crossing (just 1 AED!)"),
                    ("09:30 AM", "🧕", "Al Fahidi Historical District — Wander the oldest neighbourhood's narrow lanes and art galleries"),
                    ("11:00 AM", "🏪", "Gold Souk & Spice Souk — Haggle for gold jewellery and aromatic spices"),
                    ("01:00 PM", "🍽️", "Lunch at Arabian Tea House — Traditional Emirati dishes in a courtyard setting"),
                    ("03:00 PM", "🏨", "Rest & refresh at hotel"),
                    ("04:30 PM", "🏜️", "Desert Safari — Dune bashing, camel rides, henna, BBQ dinner under the stars"),
                ],
            },
        ],
    },
    "paris": {
        "title": "Paris",
        "days": [
            {
                "title": "Iconic Paris — Eiffel Tower & Champs-Élysées",
                "items": [
                    ("09:00 AM", "🗼", "Eiffel Tower — Summit tickets, arrive early to skip the longest queues"),
                    ("11:30 AM", "☕", "Café de Flore — Classic Parisian café in Saint-Germain-des-Prés"),
                    ("01:00 PM", "🍽️", "Lunch on Rue Cler — Charming market street with bakeries and bistros"),
                    ("03:00 PM", "🏛️", "Musée d'Orsay — Impressionist masterpieces in a stunning Beaux-Arts station"),
                    ("05:30 PM", "🚶", "Walk the Champs-Élysées to Arc de Triomphe — Climb for panoramic views"),
                    ("08:00 PM", "🍷", "Dinner in Le Marais — Trendy neighbourhood with bistros and falafel"),
                ],
            },
            {
                "title": "Art, History & Montmartre",
                "items": [
                    ("09:00 AM", "🎨", "Louvre Museum — See the Mona Lisa, Venus de Milo, and Winged Victory"),
                    ("12:30 PM", "🥖", "Lunch at Angelina — Famous for hot chocolate and Mont-Blanc pastry"),
                    ("02:30 PM", "⛪", "Sacré-Cœur & Montmartre — Hilltop basilica with the best view of Paris"),
                    ("04:00 PM", "🎨", "Place du Tertre — Watch artists paint in the historic square"),
                    ("06:00 PM", "🛍️", "Galeries Lafayette — Iconic department store with a stunning Art Nouveau dome"),
                    ("08:30 PM", "🍽️", "Dinner cruise on the Seine — Gourmet meal with illuminated landmarks"),
                ],
            },
        ],
    },
}

# Default itinerary for cities not in DB
DEFAULT_ITINERARY_TEMPLATE = [
    {
        "title": "Arrival & City Exploration",
        "items": [
            ("10:00 AM", "🏨", "Check in to your hotel and freshen up after the flight"),
            ("12:00 PM", "🍽️", "Lunch at a highly-rated local restaurant near your hotel"),
            ("02:00 PM", "🏛️", "Visit the city's most iconic landmark or museum"),
            ("04:30 PM", "☕", "Afternoon coffee break at a popular local café"),
            ("06:00 PM", "🚶", "Evening walk through the main historic district"),
            ("08:00 PM", "🍽️", "Dinner at a recommended restaurant — try the local speciality"),
        ],
    },
    {
        "title": "Culture, Markets & Local Life",
        "items": [
            ("09:00 AM", "🏪", "Explore local markets — food, crafts, and souvenirs"),
            ("11:30 AM", "🎨", "Visit a gallery, temple, or cultural site"),
            ("01:00 PM", "🍽️", "Lunch — street food or a local favourite"),
            ("03:00 PM", "🌳", "Relax in the city's best park or waterfront area"),
            ("05:00 PM", "🛍️", "Shopping in the main shopping district"),
            ("07:30 PM", "🍽️", "Farewell dinner at a top-rated restaurant"),
        ],
    },
]


# ════════════════════════════════════════════════════════════════════════════════
# DATA: HOTELS
# ════════════════════════════════════════════════════════════════════════════════

HOTEL_DB = {
    "london": [
        {"name": "The Langham", "stars": 5, "area": "Marylebone, West End", "address": "1C Portland Place, Regent Street, London W1B 1JA",
         "rating": 9.2, "rating_label": "Exceptional", "reviews": 4820,
         "price_min": 380, "price_max": 520, "currency": "GBP",
         "amenities": "Free Wi-Fi · Spa & Indoor Pool · Michelin-Star Restaurant · 24h Room Service · Concierge · Fitness Centre",
         "room_type": "Deluxe King Room", "cancel": "Free cancellation until 48h before check-in",
         "checkin": "3:00 PM", "checkout": "12:00 PM", "distance": "0.3 mi from Oxford Circus"},
        {"name": "CitizenM Tower of London", "stars": 4, "area": "City of London", "address": "40 Trinity Square, London EC3N 4DJ",
         "rating": 8.7, "rating_label": "Excellent", "reviews": 6340,
         "price_min": 145, "price_max": 210, "currency": "GBP",
         "amenities": "Free Wi-Fi · Rooftop Bar · MoodPad Room Controls · 24/7 Canteen · Self Check-In Kiosks",
         "room_type": "Standard King Room", "cancel": "Free cancellation until 24h before check-in",
         "checkin": "3:00 PM", "checkout": "11:00 AM", "distance": "0.1 mi from Tower of London"},
        {"name": "Premier Inn London County Hall", "stars": 3, "area": "South Bank, Waterloo", "address": "County Hall, Belvedere Road, London SE1 7PB",
         "rating": 8.4, "rating_label": "Very Good", "reviews": 12560,
         "price_min": 95, "price_max": 155, "currency": "GBP",
         "amenities": "Free Wi-Fi · On-Site Restaurant · Family Rooms · Tea & Coffee Maker · Air Conditioning",
         "room_type": "Double Room", "cancel": "Free cancellation until 1 day before check-in",
         "checkin": "2:00 PM", "checkout": "12:00 PM", "distance": "Steps from London Eye & Westminster"},
    ],
    "dubai": [
        {"name": "Atlantis The Royal", "stars": 5, "area": "Palm Jumeirah", "address": "Crescent Road, Palm Jumeirah, Dubai",
         "rating": 9.4, "rating_label": "Exceptional", "reviews": 3210,
         "price_min": 650, "price_max": 980, "currency": "AED",
         "amenities": "Private Beach · 17 Restaurants & Bars · Infinity Pool · Spa · Water Park Access · Butler Service",
         "room_type": "Sea View King Room", "cancel": "Free cancellation until 72h before check-in",
         "checkin": "3:00 PM", "checkout": "12:00 PM", "distance": "On Palm Jumeirah beachfront"},
        {"name": "Rove Downtown", "stars": 3, "area": "Downtown Dubai", "address": "Al Mustaqbal Street, Downtown Dubai",
         "rating": 8.6, "rating_label": "Excellent", "reviews": 8750,
         "price_min": 180, "price_max": 290, "currency": "AED",
         "amenities": "Free Wi-Fi · Rooftop Pool · The Daily Restaurant · Gym · Smart TV · Self Check-In",
         "room_type": "Rover King Room", "cancel": "Free cancellation until 24h before check-in",
         "checkin": "2:00 PM", "checkout": "12:00 PM", "distance": "0.5 mi from Burj Khalifa & Dubai Mall"},
        {"name": "JA Ocean View Hotel", "stars": 4, "area": "JBR, Dubai Marina", "address": "The Walk, Jumeirah Beach Residence, Dubai",
         "rating": 8.8, "rating_label": "Excellent", "reviews": 5430,
         "price_min": 320, "price_max": 480, "currency": "AED",
         "amenities": "Private Beach · 4 Pools · 7 Restaurants · Kids Club · Spa · Sea View Balconies",
         "room_type": "Deluxe Sea View Room", "cancel": "Free cancellation until 48h before check-in",
         "checkin": "3:00 PM", "checkout": "12:00 PM", "distance": "Beachfront on JBR Walk"},
    ],
    "paris": [
        {"name": "Le Pavillon de la Reine", "stars": 5, "area": "Le Marais, 3rd Arr.", "address": "28 Place des Vosges, 75003 Paris",
         "rating": 9.3, "rating_label": "Exceptional", "reviews": 2140,
         "price_min": 420, "price_max": 610, "currency": "EUR",
         "amenities": "Spa · Courtyard Garden · Valet Parking · Honesty Bar · Gym · Concierge · Complimentary Breakfast",
         "room_type": "Superior Double Room", "cancel": "Free cancellation until 48h before check-in",
         "checkin": "3:00 PM", "checkout": "12:00 PM", "distance": "On Place des Vosges, heart of Le Marais"},
        {"name": "Hotel Fabric", "stars": 4, "area": "Oberkampf, 11th Arr.", "address": "31 Rue de la Folie-Méricourt, 75011 Paris",
         "rating": 8.9, "rating_label": "Excellent", "reviews": 3870,
         "price_min": 160, "price_max": 240, "currency": "EUR",
         "amenities": "Free Wi-Fi · Industrial-Chic Design · Nespresso Machine · Courtyard · Rain Shower · Concierge",
         "room_type": "Classic Double Room", "cancel": "Free cancellation until 24h before check-in",
         "checkin": "3:00 PM", "checkout": "11:00 AM", "distance": "Walk to Bastille, République & nightlife"},
        {"name": "Ibis Paris Montmartre", "stars": 3, "area": "Montmartre, 18th Arr.", "address": "5 Rue Caulaincourt, 75018 Paris",
         "rating": 7.8, "rating_label": "Good", "reviews": 9240,
         "price_min": 85, "price_max": 130, "currency": "EUR",
         "amenities": "Free Wi-Fi · Bar · 24h Front Desk · Air Conditioning · Luggage Storage",
         "room_type": "Standard Double Room", "cancel": "Free cancellation until 6 PM day before",
         "checkin": "2:00 PM", "checkout": "12:00 PM", "distance": "5 min walk to Sacré-Cœur"},
    ],
    "istanbul": [
        {"name": "Four Seasons at Sultanahmet", "stars": 5, "area": "Sultanahmet, Old City", "address": "Tevkifhane Sokak No. 1, Sultanahmet, Istanbul",
         "rating": 9.5, "rating_label": "Exceptional", "reviews": 1890,
         "price_min": 350, "price_max": 550, "currency": "USD",
         "amenities": "Rooftop Restaurant · Spa · Garden Courtyard · Sea View Rooms · Concierge · Butler Service",
         "room_type": "Deluxe Room with Garden View", "cancel": "Free cancellation until 48h before check-in",
         "checkin": "3:00 PM", "checkout": "12:00 PM", "distance": "Steps from Hagia Sophia & Blue Mosque"},
        {"name": "Hotel DeCamondo Galata", "stars": 4, "area": "Galata, Beyoğlu", "address": "Kemeraltı Cad. No:44, Beyoğlu, Istanbul",
         "rating": 9.0, "rating_label": "Excellent", "reviews": 2450,
         "price_min": 120, "price_max": 200, "currency": "USD",
         "amenities": "Free Wi-Fi · Rooftop Terrace · Bosphorus Views · Breakfast Included · Rain Shower · Minibar",
         "room_type": "Superior Double Room", "cancel": "Free cancellation until 24h before check-in",
         "checkin": "2:00 PM", "checkout": "12:00 PM", "distance": "Near Galata Tower & Istiklal Avenue"},
        {"name": "Marmara Pera", "stars": 4, "area": "Tepebaşı, Beyoğlu", "address": "Meşrutiyet Caddesi, Tepebaşı, Beyoğlu, Istanbul",
         "rating": 8.5, "rating_label": "Very Good", "reviews": 6780,
         "price_min": 90, "price_max": 160, "currency": "USD",
         "amenities": "Revolving Restaurant · Pool · Spa · Fitness Centre · City & Bosphorus Views · Meeting Rooms",
         "room_type": "Standard City View Room", "cancel": "Free cancellation until 24h before check-in",
         "checkin": "3:00 PM", "checkout": "12:00 PM", "distance": "Central Beyoğlu, walk to Taksim Square"},
    ],
    "new york": [
        {"name": "The Standard High Line", "stars": 5, "area": "Meatpacking District, Manhattan", "address": "848 Washington Street, New York, NY 10014",
         "rating": 8.9, "rating_label": "Excellent", "reviews": 4560,
         "price_min": 350, "price_max": 520, "currency": "USD",
         "amenities": "Rooftop Bar · Le Bain Club · Beer Garden · Spa · Floor-to-Ceiling Windows · Hudson River Views",
         "room_type": "Superior King Room", "cancel": "Free cancellation until 48h before check-in",
         "checkin": "3:00 PM", "checkout": "12:00 PM", "distance": "Above The High Line, near Chelsea Market"},
        {"name": "Pod 51", "stars": 3, "area": "Midtown East, Manhattan", "address": "230 East 51st Street, New York, NY 10022",
         "rating": 8.1, "rating_label": "Very Good", "reviews": 11200,
         "price_min": 110, "price_max": 180, "currency": "USD",
         "amenities": "Free Wi-Fi · Rooftop Terrace · Shared Lounge · Pod Bunk Rooms · Smart TV · AC",
         "room_type": "Queen Pod", "cancel": "Free cancellation until 24h before check-in",
         "checkin": "3:00 PM", "checkout": "11:00 AM", "distance": "Walk to Grand Central & Rockefeller Center"},
        {"name": "LUMA Hotel Times Square", "stars": 4, "area": "Times Square, Midtown", "address": "120 West 41st Street, New York, NY 10036",
         "rating": 8.7, "rating_label": "Excellent", "reviews": 5890,
         "price_min": 220, "price_max": 380, "currency": "USD",
         "amenities": "Free Wi-Fi · Ortzi Restaurant · Terrace Bar · Gym · 24h Front Desk · Pet Friendly",
         "room_type": "Deluxe King Room", "cancel": "Free cancellation until 48h before check-in",
         "checkin": "3:00 PM", "checkout": "12:00 PM", "distance": "1 block from Times Square & Bryant Park"},
    ],
    "tokyo": [
        {"name": "Park Hyatt Tokyo", "stars": 5, "area": "Shinjuku", "address": "3-7-1-2 Nishi Shinjuku, Shinjuku-ku, Tokyo",
         "rating": 9.4, "rating_label": "Exceptional", "reviews": 3670,
         "price_min": 450, "price_max": 700, "currency": "JPY",
         "amenities": "New York Bar (Lost in Translation) · 47th Floor Pool · Spa · 3 Restaurants · Mt. Fuji Views · Concierge",
         "room_type": "Park Deluxe King", "cancel": "Free cancellation until 72h before check-in",
         "checkin": "3:00 PM", "checkout": "12:00 PM", "distance": "Shinjuku Station 12 min walk"},
        {"name": "Nui. Hostel & Bar Lounge", "stars": 3, "area": "Kuramae, Taito", "address": "2-14-13 Kuramae, Taito-ku, Tokyo",
         "rating": 8.5, "rating_label": "Very Good", "reviews": 7430,
         "price_min": 55, "price_max": 95, "currency": "JPY",
         "amenities": "Free Wi-Fi · Craft Beer Bar · Lounge · Shared Kitchen · Laundry · Bicycle Rental",
         "room_type": "Private Double Room", "cancel": "Free cancellation until 24h before check-in",
         "checkin": "3:00 PM", "checkout": "11:00 AM", "distance": "Trendy Kuramae, near Asakusa & Senso-ji"},
        {"name": "MUJI Hotel Ginza", "stars": 4, "area": "Ginza, Chuo", "address": "3-3-5 Ginza, Chuo-ku, Tokyo",
         "rating": 9.0, "rating_label": "Excellent", "reviews": 2980,
         "price_min": 180, "price_max": 280, "currency": "JPY",
         "amenities": "MUJI Minimalist Design · Atelier Lounge · MUJI Diner · Reading Lounge · Custom MUJI Amenities",
         "room_type": "Type E Double Room", "cancel": "Free cancellation until 48h before check-in",
         "checkin": "3:00 PM", "checkout": "11:00 AM", "distance": "Above MUJI Ginza flagship, central Ginza"},
    ],
    "singapore": [
        {"name": "Marina Bay Sands", "stars": 5, "area": "Marina Bay", "address": "10 Bayfront Avenue, Singapore 018956",
         "rating": 9.1, "rating_label": "Exceptional", "reviews": 8920,
         "price_min": 480, "price_max": 720, "currency": "SGD",
         "amenities": "Infinity Pool (57th Floor) · Casino · Celebrity Chef Restaurants · ArtScience Museum · Spa · Skypark",
         "room_type": "Deluxe Room City View", "cancel": "Free cancellation until 48h before check-in",
         "checkin": "3:00 PM", "checkout": "11:00 AM", "distance": "Iconic Marina Bay waterfront"},
        {"name": "Hotel G Singapore", "stars": 3, "area": "Bugis / Middle Road", "address": "200 Middle Road, Singapore 188980",
         "rating": 8.3, "rating_label": "Very Good", "reviews": 5430,
         "price_min": 95, "price_max": 155, "currency": "SGD",
         "amenities": "Free Wi-Fi · Ginett Restaurant · Bar · Gym · Cool Industrial Design · Near MRT",
         "room_type": "Good Double Room", "cancel": "Free cancellation until 24h before check-in",
         "checkin": "3:00 PM", "checkout": "12:00 PM", "distance": "Walk to Bugis Junction & Arab Street"},
        {"name": "The Warehouse Hotel", "stars": 4, "area": "Robertson Quay", "address": "320 Havelock Road, Singapore 169628",
         "rating": 9.0, "rating_label": "Excellent", "reviews": 2870,
         "price_min": 240, "price_max": 370, "currency": "SGD",
         "amenities": "Heritage Boutique Hotel · Po Restaurant · Rooftop Pool · Gym · Bar · River Views",
         "room_type": "Loft King Room", "cancel": "Free cancellation until 48h before check-in",
         "checkin": "3:00 PM", "checkout": "12:00 PM", "distance": "Riverside dining strip, near Clarke Quay"},
    ],
}

# Default hotels for cities not in HOTEL_DB
DEFAULT_HOTELS = [
    {"name": "Grand City Hotel", "stars": 5, "area": "City Centre",
     "rating": 9.0, "rating_label": "Excellent", "reviews": 3200,
     "price_min": 280, "price_max": 450, "currency": "USD",
     "amenities": "Free Wi-Fi · Pool · Spa · Restaurant · Fitness Centre · Concierge · Room Service",
     "room_type": "Deluxe King Room", "cancel": "Free cancellation until 48h before check-in",
     "checkin": "3:00 PM", "checkout": "12:00 PM", "distance": "City centre location"},
    {"name": "Central Park Inn", "stars": 3, "area": "Near Downtown",
     "rating": 8.2, "rating_label": "Very Good", "reviews": 7800,
     "price_min": 75, "price_max": 130, "currency": "USD",
     "amenities": "Free Wi-Fi · Restaurant · 24h Front Desk · Air Conditioning · Luggage Storage",
     "room_type": "Standard Double Room", "cancel": "Free cancellation until 24h before check-in",
     "checkin": "2:00 PM", "checkout": "12:00 PM", "distance": "Near main attractions"},
    {"name": "Boutique Quarters Hotel", "stars": 4, "area": "Trendy District",
     "rating": 8.7, "rating_label": "Excellent", "reviews": 4100,
     "price_min": 150, "price_max": 240, "currency": "USD",
     "amenities": "Free Wi-Fi · Rooftop Bar · Gym · Design Rooms · Breakfast Included · Local Art",
     "room_type": "Superior Double Room", "cancel": "Free cancellation until 48h before check-in",
     "checkin": "3:00 PM", "checkout": "11:00 AM", "distance": "Walking distance to nightlife & restaurants"},
]


# ════════════════════════════════════════════════════════════════════════════════
# FLIGHT GENERATION
# ════════════════════════════════════════════════════════════════════════════════

def generate_flights(origin_code, origin_name, dest_code, dest_name, date_str, budget=None):
    """Generate 3 realistic mock flights."""
    random.seed(hashlib.md5(f"{origin_code}{dest_code}{date_str}".encode()).hexdigest())

    # Pick 3 airlines
    pool = random.sample(AIRLINE_POOL, min(6, len(AIRLINE_POOL)))
    chosen = pool[:3]

    flights = []
    base_prices = [random.randint(420, 650), random.randint(550, 850), random.randint(380, 720)]
    base_prices.sort()

    dep_hours = [random.choice([6,7,8,9,10]), random.choice([13,14,15,16]), random.choice([20,21,22,23])]
    random.shuffle(dep_hours)

    for i, airline in enumerate(chosen):
        dep_h = dep_hours[i]
        dep_m = random.choice([0, 15, 30, 45])
        duration_h = random.randint(7, 16)
        duration_m = random.choice([0, 15, 30, 45])
        arr_h = (dep_h + duration_h) % 24
        arr_m = (dep_m + duration_m) % 60
        next_day = (dep_h + duration_h) >= 24

        is_direct = airline["hub"] == "Direct" and random.random() > 0.5
        stops = 0 if is_direct else 1
        stop_text = "Direct" if is_direct else f"1 stop · {airline['hub']}"

        price = base_prices[i]
        if budget and price > budget:
            price = budget - random.randint(10, 80)

        flight_num = f"{airline['code']}{random.randint(100,999)}"

        flights.append({
            "airline": airline["name"],
            "code": airline["code"],
            "flight_num": flight_num,
            "departure": f"{dep_h:02d}:{dep_m:02d}",
            "arrival": f"{arr_h:02d}:{arr_m:02d}" + ("+1" if next_day else ""),
            "duration": f"{duration_h}h {duration_m:02d}m",
            "stops": stops,
            "stop_text": stop_text,
            "origin_code": origin_code,
            "dest_code": dest_code,
            "origin_name": origin_name,
            "dest_name": dest_name,
            "price": price,
            "date": date_str,
            "cabin": "Economy",
        })

    # Sort by price
    flights.sort(key=lambda x: x["price"])

    # Mark best value
    best_idx = 0
    for j, f in enumerate(flights):
        score = f["price"] * 0.5 + f["stops"] * 200 - (1 if "Emirates" in f["airline"] or "Qatar" in f["airline"] or "Singapore" in f["airline"] else 0) * 100
        if j == 0 or score < best_score:
            best_score = score
            best_idx = j
    flights[best_idx]["best"] = True

    return flights


def generate_hotels(dest_name, date_str, nights=3):
    """Generate 3 realistic hotel options for the destination."""
    import copy
    dest_key = dest_name.lower()
    if dest_key in HOTEL_DB:
        hotels = copy.deepcopy(HOTEL_DB[dest_key])
    else:
        hotels = copy.deepcopy(DEFAULT_HOTELS)
        for h in hotels:
            h["area"] = f"{dest_name} {h['area']}"

    random.seed(hashlib.md5(f"hotel_{dest_key}_{date_str}".encode()).hexdigest())

    for h in hotels:
        h["price_per_night"] = random.randint(h["price_min"], h["price_max"])
        h["total_price"] = h["price_per_night"] * nights
        h["nights"] = nights

    # Sort by price
    hotels.sort(key=lambda x: x["price_per_night"])

    # Mark best value
    best_idx = 0
    best_score = None
    for j, h in enumerate(hotels):
        score = h["price_per_night"] * 0.4 - h["rating"] * 50 + (5 - h["stars"]) * 30
        if best_score is None or score < best_score:
            best_score = score
            best_idx = j
    hotels[best_idx]["top_pick"] = True

    return hotels


# ════════════════════════════════════════════════════════════════════════════════
# INTENT PARSING
# ════════════════════════════════════════════════════════════════════════════════

def find_city(text):
    """Find a city name in text."""
    text_lower = text.lower()
    for city_name, (code, display) in sorted(CITY_DB.items(), key=lambda x: -len(x[0])):
        if city_name in text_lower:
            return code, display
    return None, None

def parse_trip_request(text):
    """Extract origin, destination, date, budget from user message."""
    text_lower = text.lower()

    origin_code, origin_name = None, None
    dest_code, dest_name = None, None
    date_str = None
    budget = None

    # Try "from X to Y" pattern
    match = re.search(r'from\s+(.+?)\s+to\s+(.+?)(?:\s+on|\s+next|\s+for|\s+under|\s+around|\s+in|\s+this|\s*$)', text_lower)
    if match:
        for city, (code, display) in sorted(CITY_DB.items(), key=lambda x: -len(x[0])):
            if city in match.group(1) and not origin_code:
                origin_code, origin_name = code, display
            if city in match.group(2) and not dest_code:
                dest_code, dest_name = code, display

    # Fallback: find any two cities in the text
    if not origin_code or not dest_code:
        found = []
        for city, (code, display) in sorted(CITY_DB.items(), key=lambda x: -len(x[0])):
            pos = text_lower.find(city)
            if pos != -1 and (code, display) not in found:
                found.append((code, display, pos))
        found.sort(key=lambda x: x[2])
        if len(found) >= 2:
            origin_code, origin_name = found[0][0], found[0][1]
            dest_code, dest_name = found[1][0], found[1][1]
        elif len(found) == 1:
            dest_code, dest_name = found[0][0], found[0][1]

    # Parse budget
    budget_match = re.search(r'(?:under|below|max|budget|within)\s*\$?\s*(\d+)', text_lower)
    if not budget_match:
        budget_match = re.search(r'\$(\d+)', text_lower)
    if budget_match:
        budget = int(budget_match.group(1))

    # Parse date
    today = datetime.now()

    days_of_week = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    for day_name, day_num in days_of_week.items():
        if day_name in text_lower:
            days_ahead = (day_num - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7  # next occurrence
            if "next" in text_lower:
                days_ahead += 7 if days_ahead <= 7 else 0
            target = today + timedelta(days=days_ahead)
            date_str = target.strftime("%A, %B %d, %Y")
            break

    if not date_str:
        # Try month + day patterns
        months = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
            "jan": 1, "feb": 2, "mar": 3, "apr": 4,
            "jun": 6, "jul": 7, "aug": 8, "sep": 9,
            "oct": 10, "nov": 11, "dec": 12,
        }
        for mname, mnum in months.items():
            pattern = rf'{mname}\s+(\d{{1,2}})'
            m = re.search(pattern, text_lower)
            if m:
                day = int(m.group(1))
                year = today.year if mnum >= today.month else today.year + 1
                try:
                    target = datetime(year, mnum, day)
                    date_str = target.strftime("%A, %B %d, %Y")
                except ValueError:
                    pass
                break

    if not date_str:
        if "tomorrow" in text_lower:
            date_str = (today + timedelta(days=1)).strftime("%A, %B %d, %Y")
        elif "today" in text_lower:
            date_str = today.strftime("%A, %B %d, %Y")
        else:
            # Default to next week
            target = today + timedelta(days=7)
            date_str = target.strftime("%A, %B %d, %Y")

    return {
        "origin_code": origin_code, "origin_name": origin_name,
        "dest_code": dest_code, "dest_name": dest_name,
        "date": date_str, "budget": budget,
    }

def parse_selection(text):
    """Extract flight option number (1, 2, or 3) from user message."""
    text_lower = text.lower()
    ordinals = {"first": 1, "second": 2, "third": 3, "1st": 1, "2nd": 2, "3rd": 3}
    for word, num in ordinals.items():
        if word in text_lower:
            return num
    match = re.search(r'(?:option|flight|number|#|book)\s*(\d)', text_lower)
    if match:
        return int(match.group(1))
    for ch in text:
        if ch in "123":
            return int(ch)
    return None

def is_affirmative(text):
    text_lower = text.lower().strip()
    positives = ["yes", "yeah", "yep", "sure", "confirm", "proceed", "go ahead",
                 "do it", "pay", "book it", "ok", "okay", "absolutely", "let's go",
                 "approved", "affirmative", "yup", "please", "y", "si", "haan", "ji"]
    return any(p in text_lower for p in positives)

def is_negative(text):
    text_lower = text.lower().strip()
    negatives = ["no", "nope", "cancel", "stop", "don't", "nah", "abort",
                 "never mind", "forget it", "skip"]
    return any(n in text_lower for n in negatives)

def wants_itinerary(text):
    text_lower = text.lower()
    return is_affirmative(text) or any(w in text_lower for w in ["itinerary", "plan", "schedule", "activities"])


# ════════════════════════════════════════════════════════════════════════════════
# HTML CARD RENDERERS
# ════════════════════════════════════════════════════════════════════════════════

def render_flight_card(flight, idx):
    best_html = '<span class="best-badge">★ BEST VALUE</span>' if flight.get("best") else ""
    return (
        '<div class="flight-card">'
        f'<span class="option-badge">Option {idx}</span>{best_html}'
        '<div class="airline-row">'
        f'<span class="airline-name">{flight["airline"]}</span>'
        f'<span class="airline-code">{flight["flight_num"]} · {flight["cabin"]}</span>'
        '</div>'
        '<div class="route-row">'
        '<div class="route-point">'
        f'<div class="route-time">{flight["departure"]}</div>'
        f'<div class="route-code">{flight["origin_name"]} ({flight["origin_code"]})</div>'
        '</div>'
        '<div class="route-line-wrapper">'
        f'<div class="route-duration">{flight["duration"]}</div>'
        '<div class="route-line"></div>'
        f'<div class="route-stops">{flight["stop_text"]}</div>'
        '</div>'
        '<div class="route-point">'
        f'<div class="route-time">{flight["arrival"]}</div>'
        f'<div class="route-code">{flight["dest_name"]} ({flight["dest_code"]})</div>'
        '</div>'
        '</div>'
        '<div style="display:flex;justify-content:space-between;align-items:flex-end;margin-top:4px;">'
        f'<div class="price-tag">${flight["price"]}</div>'
        '<div class="price-note">per person · incl. taxes</div>'
        '</div>'
        '</div>'
    )

def render_payment_card(flight, booking_ref):
    base = int(flight["price"] * 0.78)
    taxes = int(flight["price"] * 0.15)
    fees = flight["price"] - base - taxes
    return (
        '<div class="payment-card">'
        '<h3>💳 Payment Summary</h3>'
        f'<div class="payment-subtitle">Booking Ref: {booking_ref}</div>'
        '<div class="pay-line">'
        f'<span class="pay-line-label">{flight["airline"]} · {flight["flight_num"]}</span>'
        f'<span class="pay-line-value">{flight["origin_code"]} → {flight["dest_code"]}</span>'
        '</div>'
        '<div class="pay-line">'
        '<span class="pay-line-label">Date</span>'
        f'<span class="pay-line-value">{flight["date"]}</span>'
        '</div>'
        '<div class="pay-line">'
        '<span class="pay-line-label">Base Fare</span>'
        f'<span class="pay-line-value">${base}.00</span>'
        '</div>'
        '<div class="pay-line">'
        '<span class="pay-line-label">Taxes &amp; Surcharges</span>'
        f'<span class="pay-line-value">${taxes}.00</span>'
        '</div>'
        '<div class="pay-line">'
        '<span class="pay-line-label">Service Fee</span>'
        f'<span class="pay-line-value">${fees}.00</span>'
        '</div>'
        '<div class="pay-total">'
        '<span class="pay-total-label">Total</span>'
        f'<span class="pay-total-value">${flight["price"]}.00 USD</span>'
        '</div>'
        '<div class="pay-card-info">'
        '<span class="pay-card-icon">💎</span>'
        '<div>'
        '<div class="pay-card-label">PAYMENT METHOD</div>'
        '<div class="pay-card-number">Visa •••• •••• •••• 4242 (Citi Travel Card)</div>'
        '</div>'
        '</div>'
        '</div>'
    )

def render_confirmation_card(flight, booking_ref, pnr):
    txn_id = hashlib.md5(booking_ref.encode()).hexdigest()[:12]
    return (
        '<div class="confirm-card">'
        '<div class="confirm-icon">✅</div>'
        '<div class="confirm-title">Payment Successful!</div>'
        '<div class="confirm-sub">Your flight has been booked and confirmed</div>'
        '<div class="confirm-details">'
        '<div>'
        '<div class="confirm-item-label">PNR / Booking Code</div>'
        f'<div class="confirm-item-value">{pnr}</div>'
        '</div>'
        '<div>'
        '<div class="confirm-item-label">Flight</div>'
        f'<div class="confirm-item-value">{flight["airline"]} {flight["flight_num"]}</div>'
        '</div>'
        '<div>'
        '<div class="confirm-item-label">Route</div>'
        f'<div class="confirm-item-value">{flight["origin_name"]} → {flight["dest_name"]}</div>'
        '</div>'
        '<div>'
        '<div class="confirm-item-label">Date</div>'
        f'<div class="confirm-item-value">{flight["date"]}</div>'
        '</div>'
        '<div>'
        '<div class="confirm-item-label">Departure</div>'
        f'<div class="confirm-item-value">{flight["departure"]} ({flight["origin_code"]})</div>'
        '</div>'
        '<div>'
        '<div class="confirm-item-label">Arrival</div>'
        f'<div class="confirm-item-value">{flight["arrival"]} ({flight["dest_code"]})</div>'
        '</div>'
        '<div>'
        '<div class="confirm-item-label">Amount Charged</div>'
        f'<div class="confirm-item-value">${flight["price"]}.00 USD</div>'
        '</div>'
        '<div>'
        '<div class="confirm-item-label">Transaction ID</div>'
        f'<div class="confirm-item-value">txn_{txn_id}</div>'
        '</div>'
        '</div>'
        '</div>'
    )

def render_itinerary(dest_name, num_days=None):
    import copy
    dest_key = dest_name.lower()
    if dest_key in ITINERARY_DB:
        data = ITINERARY_DB[dest_key]
        days = copy.deepcopy(data["days"])
    else:
        days = copy.deepcopy(DEFAULT_ITINERARY_TEMPLATE)
        for d in days:
            for idx, (t, e, desc) in enumerate(d["items"]):
                d["items"][idx] = (t, e, desc.replace("the city's", f"{dest_name}'s"))

    if num_days:
        days = days[:num_days]

    html = ""
    for i, day in enumerate(days, 1):
        items_html = ""
        for t, emoji, desc in day["items"]:
            items_html += (
                '<div class="itin-item">'
                f'<span class="itin-time">{t}</span>'
                f'<span class="itin-emoji">{emoji}</span>'
                f'<span class="itin-desc">{desc}</span>'
                '</div>'
            )
        html += (
            '<div class="itin-day">'
            '<div class="itin-day-header">'
            f'<div class="itin-day-num">{i}</div>'
            f'<div class="itin-day-title">Day {i} — {day["title"]}</div>'
            '</div>'
            f'{items_html}'
            '</div>'
        )
    return html


def render_hotel_card(hotel, idx):
    star_str = "★" * hotel["stars"] + "☆" * (5 - hotel["stars"])
    top_html = '<span class="hotel-top-badge">★ TOP PICK</span>' if hotel.get("top_pick") else ""
    cancel_class = "hotel-cancel-policy"
    return (
        '<div class="hotel-card">'
        f'<span class="hotel-option-badge">Option {idx}</span>{top_html}'
        '<div class="hotel-name-row">'
        f'<span class="hotel-name">{hotel["name"]}</span>'
        '</div>'
        f'<div class="hotel-stars">{star_str}</div>'
        f'<div class="hotel-address">📍 {hotel["area"]} · {hotel.get("distance", "")}</div>'
        '<div class="hotel-details-row">'
        f'<span class="hotel-detail-chip">🛏️ {hotel["room_type"]}</span>'
        f'<span class="hotel-detail-chip">📅 {hotel["nights"]} nights</span>'
        f'<span class="hotel-detail-chip">🕐 Check-in {hotel["checkin"]}</span>'
        '</div>'
        f'<div class="hotel-amenities">🏨 {hotel["amenities"]}</div>'
        '<div class="hotel-bottom-row">'
        '<div class="hotel-rating">'
        f'<span class="hotel-rating-score">{hotel["rating"]}</span>'
        '<div>'
        f'<div class="hotel-rating-label">{hotel["rating_label"]}</div>'
        f'<div class="hotel-rating-reviews">{hotel["reviews"]:,} reviews</div>'
        '</div>'
        '</div>'
        '<div class="hotel-price-block">'
        f'<div class="hotel-price">${hotel["price_per_night"]}</div>'
        f'<div class="hotel-price-note">per night · {hotel["currency"]} · ${hotel["total_price"]} total</div>'
        f'<div class="{cancel_class}">{hotel["cancel"]}</div>'
        '</div>'
        '</div>'
        '</div>'
    )


def render_hotel_payment_card(hotel, booking_ref):
    room_cost = hotel["price_per_night"] * hotel["nights"]
    taxes = int(room_cost * 0.13)
    service_fee = int(room_cost * 0.05)
    total = room_cost + taxes + service_fee
    return (
        '<div class="hotel-payment-card">'
        '<h3>🏨 Hotel Payment Summary</h3>'
        f'<div class="payment-subtitle">Booking Ref: {booking_ref}</div>'
        '<div class="pay-line">'
        f'<span class="pay-line-label">{hotel["name"]}</span>'
        f'<span class="pay-line-value">{hotel["stars"]}★ · {hotel["area"]}</span>'
        '</div>'
        '<div class="pay-line">'
        '<span class="pay-line-label">Room Type</span>'
        f'<span class="pay-line-value">{hotel["room_type"]}</span>'
        '</div>'
        '<div class="pay-line">'
        '<span class="pay-line-label">Duration</span>'
        f'<span class="pay-line-value">{hotel["nights"]} nights</span>'
        '</div>'
        '<div class="pay-line">'
        f'<span class="pay-line-label">Room Rate ({hotel["nights"]} × ${hotel["price_per_night"]})</span>'
        f'<span class="pay-line-value">${room_cost}.00</span>'
        '</div>'
        '<div class="pay-line">'
        '<span class="pay-line-label">Taxes &amp; Local Fees (13%)</span>'
        f'<span class="pay-line-value">${taxes}.00</span>'
        '</div>'
        '<div class="pay-line">'
        '<span class="pay-line-label">Service Fee</span>'
        f'<span class="pay-line-value">${service_fee}.00</span>'
        '</div>'
        '<div class="pay-total">'
        '<span class="pay-total-label">Total</span>'
        f'<span class="pay-total-value">${total}.00 {hotel["currency"]}</span>'
        '</div>'
        '<div class="pay-card-info">'
        '<span class="pay-card-icon">💎</span>'
        '<div>'
        '<div class="pay-card-label">PAYMENT METHOD</div>'
        '<div class="pay-card-number">Visa •••• •••• •••• 4242 (Citi Travel Card)</div>'
        '</div>'
        '</div>'
        '</div>'
    )


def render_hotel_confirmation_card(hotel, booking_ref, confirmation_id):
    room_cost = hotel["price_per_night"] * hotel["nights"]
    taxes = int(room_cost * 0.13)
    service_fee = int(room_cost * 0.05)
    total = room_cost + taxes + service_fee
    txn_id = hashlib.md5(f"hotel_{booking_ref}".encode()).hexdigest()[:12]
    return (
        '<div class="hotel-confirm-card">'
        '<div class="confirm-icon">🏨</div>'
        '<div class="confirm-title">Hotel Reservation Confirmed!</div>'
        '<div class="confirm-sub">Your room has been booked successfully</div>'
        '<div class="confirm-details">'
        '<div>'
        '<div class="confirm-item-label">Confirmation ID</div>'
        f'<div class="confirm-item-value">{confirmation_id}</div>'
        '</div>'
        '<div>'
        '<div class="confirm-item-label">Hotel</div>'
        f'<div class="confirm-item-value">{hotel["name"]}</div>'
        '</div>'
        '<div>'
        '<div class="confirm-item-label">Room</div>'
        f'<div class="confirm-item-value">{hotel["room_type"]}</div>'
        '</div>'
        '<div>'
        '<div class="confirm-item-label">Duration</div>'
        f'<div class="confirm-item-value">{hotel["nights"]} nights</div>'
        '</div>'
        '<div>'
        '<div class="confirm-item-label">Check-in</div>'
        f'<div class="confirm-item-value">{hotel["checkin"]}</div>'
        '</div>'
        '<div>'
        '<div class="confirm-item-label">Check-out</div>'
        f'<div class="confirm-item-value">{hotel["checkout"]}</div>'
        '</div>'
        '<div>'
        '<div class="confirm-item-label">Amount Charged</div>'
        f'<div class="confirm-item-value">${total}.00 {hotel["currency"]}</div>'
        '</div>'
        '<div>'
        '<div class="confirm-item-label">Transaction ID</div>'
        f'<div class="confirm-item-value">txn_{txn_id}</div>'
        '</div>'
        '</div>'
        '</div>'
    )


# ════════════════════════════════════════════════════════════════════════════════
# CONVERSATION ENGINE
# ════════════════════════════════════════════════════════════════════════════════

def process_message(user_input):
    """Main state machine with LLM-enhanced parsing. Falls back to rule-based if LLM unavailable."""
    stage = st.session_state.stage
    responses = []

    # ── LLM PARSING (with fallback) ──
    parsed = llm_parse(user_input, stage)
    use_llm = parsed is not None

    # Helper: get yes/no from LLM or fallback
    def check_yes():
        if use_llm:
            return parsed.get("is_yes", False)
        return is_affirmative(user_input)

    def check_no():
        if use_llm:
            return parsed.get("is_no", False)
        return is_negative(user_input)

    def get_selection():
        if use_llm and parsed.get("selection"):
            return parsed["selection"]
        return parse_selection(user_input)

    def get_intent():
        if use_llm:
            return parsed.get("intent", "")
        return ""

    # ── HANDLE CROSS-CUTTING INTENTS AT ANY STAGE ──
    # These intents should work regardless of what stage the user is in
    if use_llm:
        intent = get_intent()
        safe_stages = ("payment_confirm", "hotel_payment_confirm")  # Don't interrupt active payment

        # General conversation
        if intent == "general" and stage not in safe_stages:
            reply = parsed.get("agent_reply", "")
            if reply:
                responses.append(("text", reply))
                return responses

        # Cancel booking — works at any stage after a booking exists
        if intent == "cancel_booking" and stage not in safe_stages:
            if st.session_state.pnr and st.session_state.selected_flight:
                pnr = st.session_state.pnr
                flight = st.session_state.selected_flight
                responses.append(("text",
                    f"I can help with that. For flight booking **{pnr}** "
                    f"({flight['airline']} {flight['flight_num']}, "
                    f"{flight['origin_name']} → {flight['dest_name']}):\n\n"
                    f"✅ This booking is currently within the **free cancellation window**. "
                    f"A full refund of **${flight['price']}.00** "
                    f"would be issued to your Visa ending in 4242.\n\n"
                    f"Shall I proceed with the cancellation? The refund typically takes "
                    f"5–10 business days to appear on your statement."
                ))
                st.session_state.stage = "complete"
                return responses
            else:
                responses.append(("text", "You don't have any active flight bookings to cancel. Would you like to search for flights?"))
                return responses

        # Cancel hotel — works at any stage after a hotel is booked
        if intent == "cancel_hotel" and stage not in safe_stages:
            if st.session_state.selected_hotel:
                hotel = st.session_state.selected_hotel
                conf_id = st.session_state.hotel_confirmation_id or "N/A"
                room_cost = hotel["price_per_night"] * hotel["nights"]
                taxes = int(room_cost * 0.13)
                service_fee = int(room_cost * 0.05)
                total = room_cost + taxes + service_fee
                responses.append(("text",
                    f"I can help with that. For hotel reservation **{conf_id}** "
                    f"({hotel['name']}, {hotel['stars']}★ in {hotel['area']}, "
                    f"{hotel['room_type']} for {hotel['nights']} nights):\n\n"
                    f"✅ This reservation is within the **{hotel['cancel'].lower()}** window. "
                    f"A full refund of **${total}.00 {hotel['currency']}** "
                    f"would be issued to your Visa ending in 4242.\n\n"
                    f"Shall I proceed with the hotel cancellation? The refund typically takes "
                    f"5–10 business days to appear on your statement."
                ))
                st.session_state.stage = "complete"
                return responses
            else:
                responses.append(("text", "You don't have any active hotel reservations to cancel. Would you like to search for hotels?"))
                return responses

        # Check booking status — works at any stage
        if intent == "check_status" and stage not in safe_stages:
            if st.session_state.pnr:
                status_parts = [f"📊 **Booking Status for {st.session_state.pnr}:**\n"]
                status_parts.append(f"• Flight: **Confirmed** ✅")
                status_parts.append(f"• Payment: **Completed** ✅")
                status_parts.append(f"• E-ticket: **Delivered** ✅")
                if st.session_state.selected_hotel:
                    h = st.session_state.selected_hotel
                    status_parts.append(f"• Hotel ({h['name']}): **Confirmed** ✅")
                status_parts.append(f"\nEverything looks good! Need anything else?")
                responses.append(("text", "\n".join(status_parts)))
            else:
                responses.append(("text", "You don't have any active bookings yet. Want to search for flights?"))
            return responses

        # New search / start over — works at any stage except active payment
        if intent in ("new_search", "search_flight") and stage not in safe_stages and stage not in ("greeting", "searching"):
            # If it's a search_flight with a destination, restart and process it
            if intent == "search_flight" and parsed.get("destination"):
                st.session_state.stage = "greeting"
                st.session_state.flights = []
                st.session_state.selected_flight = None
                st.session_state.hotels = []
                st.session_state.selected_hotel = None
                return process_message(user_input)
            elif intent == "new_search":
                st.session_state.stage = "greeting"
                st.session_state.flights = []
                st.session_state.selected_flight = None
                st.session_state.hotels = []
                st.session_state.selected_hotel = None
                responses.append(("text", "Let's plan another trip! Where would you like to go?"))
                return responses

    # ═══════════════════════════════════════════════════════
    # STAGE: GREETING / SEARCHING
    # ═══════════════════════════════════════════════════════
    if stage == "greeting" or stage == "searching":

        # ── LLM PATH ──
        if use_llm:
            intent = get_intent()
            origin_raw = parsed.get("origin")
            dest_raw = parsed.get("destination")
            origin_ok = parsed.get("origin_supported", True)
            dest_ok = parsed.get("destination_supported", True)
            date_str = parsed.get("date")
            budget = parsed.get("budget")

            # Handle non-search intents
            if intent == "general" and parsed.get("agent_reply"):
                responses.append(("text", parsed["agent_reply"]))
                return responses

            if intent == "greeting":
                responses.append(("text",
                    "Hey there! 👋 Ready to plan your next trip? "
                    "Just tell me where you'd like to go — for example: "
                    "*\"Find me flights from Lahore to London next Friday under $800\"*"
                ))
                return responses

            # ── DATE VALIDATION (check past dates BEFORE anything else) ──
            date_valid = parsed.get("date_valid", True)
            date_error = parsed.get("date_error")

            if date_error == "past_date" or (date_str and not date_valid):
                raw_date = date_str or "that date"
                extra = ""
                if not dest_raw:
                    extra = " Also, please let me know your destination city."
                responses.append(("text",
                    f"It looks like **{raw_date}** is in the past — I can only search for future travel dates. "
                    f"Could you give me an upcoming date?{extra} For example:\n\n"
                    f"• *\"Find me flights from Lahore to London next Friday\"*\n"
                    f"• *\"Book a flight to Dubai on April 20, 2026\"*"
                ))
                return responses

            # Destination not provided
            if not dest_raw:
                responses.append(("text",
                    "I'd love to help you find flights! Could you tell me where you'd like to go? "
                    "For example: *\"Find me flights from Lahore to London next Friday under $800\"*"
                ))
                return responses

            # Destination not in our database
            if dest_raw and not dest_ok:
                reply = parsed.get("agent_reply", "")
                if reply:
                    responses.append(("text", reply))
                else:
                    supported = ", ".join(sorted(set(d for _, (_, d) in CITY_DB.items())))
                    responses.append(("text",
                        f"I'm sorry, I don't have flight data for **{dest_raw}** yet. "
                        f"I currently support these cities:\n\n{supported}\n\n"
                        f"Would you like to search for flights to one of these destinations?"
                    ))
                return responses

            # Origin not in database
            if origin_raw and not origin_ok:
                reply = parsed.get("agent_reply", "")
                if reply:
                    responses.append(("text", reply))
                else:
                    responses.append(("text",
                        f"I don't have departure data for **{origin_raw}** yet. "
                        f"Would you like to depart from a different city? "
                        f"I support Lahore, Karachi, Islamabad, Dubai, London, and many more."
                    ))
                return responses

            # Match cities to database
            dest_code, dest_name = match_city_name(dest_raw)
            if not dest_code:
                responses.append(("text",
                    f"I couldn't find **{dest_raw}** in my database. Could you double-check the city name?"
                ))
                return responses

            if origin_raw:
                origin_code, origin_name = match_city_name(origin_raw)
                if not origin_code:
                    origin_code, origin_name = "LHE", "Lahore"
            else:
                origin_code, origin_name = "LHE", "Lahore"

            # ── DATE VALIDATION (remaining checks after cities resolved) ──
            # Vague date
            if date_error == "vague_date":
                responses.append(("text",
                    f"I'd love to help, but I need a more specific travel date. "
                    f"Could you tell me when you'd like to fly? For example:\n\n"
                    f"• *\"next Friday\"*\n"
                    f"• *\"June 15\"*\n"
                    f"• *\"tomorrow\"*\n"
                    f"• *\"this Saturday\"*"
                ))
                return responses

            # No date provided — ask for it
            if date_error == "no_date" or not date_str:
                responses.append(("text",
                    f"Great — I can search flights from **{origin_name}** to **{dest_name}**! "
                    f"When would you like to travel? Just give me a date, like:\n\n"
                    f"• *\"next Friday\"*\n"
                    f"• *\"April 20\"*\n"
                    f"• *\"this weekend\"*"
                ))
                # Save partial trip so the next message only needs a date
                st.session_state.trip = {
                    "origin_code": origin_code, "origin_name": origin_name,
                    "dest_code": dest_code, "dest_name": dest_name,
                    "date": None, "budget": budget,
                }
                st.session_state.stage = "awaiting_date"
                return responses

            trip = {
                "origin_code": origin_code, "origin_name": origin_name,
                "dest_code": dest_code, "dest_name": dest_name,
                "date": date_str, "budget": budget,
            }

        # ── RULE-BASED FALLBACK ──
        else:
            trip = parse_trip_request(user_input)

            if not trip["dest_code"]:
                responses.append(("text",
                    "I'd love to help you find flights! Could you tell me where you'd like to go? "
                    "For example: *\"Find me flights from Lahore to London next Friday under $800\"*"
                ))
                return responses

            if not trip["origin_code"]:
                trip["origin_code"] = "LHE"
                trip["origin_name"] = "Lahore"

        # ── COMMON: Search and display flights ──
        st.session_state.trip = trip

        budget_note = f" within your **${trip['budget']}** budget" if trip.get("budget") else ""
        responses.append(("text",
            f"Got it! Let me search for flights from **{trip['origin_name']} ({trip['origin_code']})** "
            f"to **{trip['dest_name']} ({trip['dest_code']})** on **{trip['date']}**{budget_note}."
        ))
        responses.append(("thinking", "Searching 200+ flights across 15 airlines..."))

        flights = generate_flights(
            trip["origin_code"], trip["origin_name"],
            trip["dest_code"], trip["dest_name"],
            trip["date"], trip.get("budget"),
        )
        st.session_state.flights = flights

        responses.append(("text", f"I found **{len(flights)} great options** for you:\n"))

        cards_html = ""
        for i, f in enumerate(flights, 1):
            cards_html += render_flight_card(f, i)
        responses.append(("html", cards_html))

        best = [f for f in flights if f.get("best")]
        if best:
            b = best[0]
            idx = flights.index(b) + 1
            reason = "best balance of price, flight time, and airline quality"
            responses.append(("text",
                f"💡 **My recommendation:** Option {idx} ({b['airline']}) — {reason}.\n\n"
                f"To book, just say **\"Book option 1\"**, **\"Book option 2\"**, or **\"Book option 3\"**. "
                f"Or tell me if you'd like to adjust the search."
            ))

        st.session_state.stage = "flights_shown"

    # ═══════════════════════════════════════════════════════
    # STAGE: AWAITING DATE
    # ═══════════════════════════════════════════════════════
    elif stage == "awaiting_date":
        # User is providing a date for an already-identified route
        trip = st.session_state.trip
        date_str = None
        date_error = None

        if use_llm:
            date_str = parsed.get("date")
            date_error = parsed.get("date_error")
            date_valid = parsed.get("date_valid", True)

            if date_error == "past_date" or not date_valid:
                raw_date = date_str or "that date"
                responses.append(("text",
                    f"**{raw_date}** is in the past — I need a future date. When would you like to fly?"
                ))
                return responses

            if date_error == "vague_date" or (not date_str and date_error != "no_date"):
                responses.append(("text",
                    f"Could you be a bit more specific? Try something like *\"next Friday\"* or *\"April 20\"*."
                ))
                return responses

            if not date_str:
                responses.append(("text",
                    f"I still need a travel date. When would you like to fly from "
                    f"**{trip['origin_name']}** to **{trip['dest_name']}**?"
                ))
                return responses
        else:
            # Rule-based date extraction
            fallback_trip = parse_trip_request(user_input)
            date_str = fallback_trip.get("date")
            if not date_str:
                target = datetime.now() + timedelta(days=7)
                date_str = target.strftime("%A, %B %d, %Y")

        trip["date"] = date_str
        st.session_state.trip = trip
        st.session_state.stage = "greeting"

        # Now process as a normal search
        budget_note = f" within your **${trip['budget']}** budget" if trip.get("budget") else ""
        responses.append(("text",
            f"Got it! Let me search for flights from **{trip['origin_name']} ({trip['origin_code']})** "
            f"to **{trip['dest_name']} ({trip['dest_code']})** on **{trip['date']}**{budget_note}."
        ))
        responses.append(("thinking", "Searching 200+ flights across 15 airlines..."))

        flights = generate_flights(
            trip["origin_code"], trip["origin_name"],
            trip["dest_code"], trip["dest_name"],
            trip["date"], trip.get("budget"),
        )
        st.session_state.flights = flights

        responses.append(("text", f"I found **{len(flights)} great options** for you:\n"))

        cards_html = ""
        for i, f in enumerate(flights, 1):
            cards_html += render_flight_card(f, i)
        responses.append(("html", cards_html))

        best = [f for f in flights if f.get("best")]
        if best:
            b = best[0]
            idx = flights.index(b) + 1
            reason = "best balance of price, flight time, and airline quality"
            responses.append(("text",
                f"💡 **My recommendation:** Option {idx} ({b['airline']}) — {reason}.\n\n"
                f"To book, just say **\"Book option 1\"**, **\"Book option 2\"**, or **\"Book option 3\"**. "
                f"Or tell me if you'd like to adjust the search."
            ))

        st.session_state.stage = "flights_shown"

    # ═══════════════════════════════════════════════════════
    # STAGE: FLIGHTS SHOWN
    # ═══════════════════════════════════════════════════════
    elif stage == "flights_shown":
        selection = get_selection()
        if selection and 1 <= selection <= len(st.session_state.flights):
            flight = st.session_state.flights[selection - 1]
            st.session_state.selected_flight = flight
            booking_ref = f"SKY-{random.randint(100000, 999999)}"
            st.session_state.booking_ref = booking_ref

            responses.append(("text",
                f"Excellent choice! **{flight['airline']} {flight['flight_num']}** — "
                f"departing {flight['departure']} from {flight['origin_code']}, "
                f"arriving {flight['arrival']} at {flight['dest_code']}.\n\n"
                f"Here's your payment summary:"
            ))
            responses.append(("html", render_payment_card(flight, booking_ref)))
            responses.append(("text",
                f"**Shall I proceed with this payment of ${flight['price']}.00 USD "
                f"from your Visa ending in 4242?**\n\n"
                f"Say **\"Confirm\"** to proceed or **\"Cancel\"** to go back."
            ))
            st.session_state.stage = "payment_confirm"
        else:
            responses.append(("text",
                "Which flight would you like to book? Just say **\"Book option 1\"**, "
                "**\"Book option 2\"**, or **\"Book option 3\"**."
            ))

    # ═══════════════════════════════════════════════════════
    # STAGE: PAYMENT CONFIRM
    # ═══════════════════════════════════════════════════════
    elif stage == "payment_confirm":
        if check_yes():
            flight = st.session_state.selected_flight
            booking_ref = st.session_state.booking_ref
            pnr = ''.join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=6))
            st.session_state.pnr = pnr

            responses.append(("thinking", "Verifying 2FA session... ✓"))
            responses.append(("thinking", "Processing payment via Stripe..."))
            responses.append(("thinking", "Confirming booking with airline..."))

            responses.append(("html", render_confirmation_card(flight, booking_ref, pnr)))
            responses.append(("text",
                f"🎉 **Your flight is booked!** A confirmation email with your e-ticket "
                f"has been sent to your registered email address.\n\n"
                f"Now let's find you a place to stay! Would you like me to **search for hotels** "
                f"in **{flight['dest_name']}** for your trip?"
            ))
            st.session_state.stage = "offer_hotel"

        elif check_no():
            responses.append(("text",
                "No problem — payment cancelled. No charge has been made.\n\n"
                "Would you like to pick a different flight, or start a new search?"
            ))
            st.session_state.stage = "flights_shown"
        else:
            responses.append(("text",
                "I need a clear confirmation before processing the payment. "
                "Please say **\"Confirm\"** to proceed or **\"Cancel\"** to go back."
            ))

    # ═══════════════════════════════════════════════════════
    # STAGE: OFFER HOTEL
    # ═══════════════════════════════════════════════════════
    elif stage == "offer_hotel":
        wants_hotel = check_yes() or (use_llm and get_intent() in ("ask_hotel", "confirm"))
        skip = check_no() or (use_llm and get_intent() == "skip")

        if not use_llm:
            wants_hotel = wants_hotel or any(w in user_input.lower() for w in ["hotel", "stay", "room", "find"])
            skip = skip or "skip" in user_input.lower()

        if wants_hotel:
            flight = st.session_state.selected_flight
            dest = flight["dest_name"]

            responses.append(("thinking", f"Searching 400+ hotels in {dest}..."))
            responses.append(("thinking", "Checking availability & rates..."))

            hotels = generate_hotels(dest, flight["date"])
            st.session_state.hotels = hotels

            responses.append(("text",
                f"I found **{len(hotels)} great options** in {dest} for **{hotels[0]['nights']} nights**. "
                f"Here are my top picks, sorted by price:\n"
            ))

            cards_html = ""
            for i, h in enumerate(hotels, 1):
                cards_html += render_hotel_card(h, i)
            responses.append(("html", cards_html))

            top = [h for h in hotels if h.get("top_pick")]
            if top:
                t = top[0]
                idx = hotels.index(t) + 1
                responses.append(("text",
                    f"💡 **My recommendation:** Option {idx} ({t['name']}) — "
                    f"rated {t['rating']}/10 ({t['rating_label']}), great location at {t['area']}, "
                    f"and {t['cancel'].lower()}.\n\n"
                    f"To book, just say **\"Book hotel 1\"**, **\"Book hotel 2\"**, or **\"Book hotel 3\"**. "
                    f"Or say **\"skip\"** to move on to itinerary planning."
                ))

            st.session_state.stage = "hotels_shown"

        elif skip:
            flight = st.session_state.selected_flight
            responses.append(("text",
                f"No worries! Your flight is all set. Would you like me to "
                f"**generate a day-by-day itinerary** for your trip to {flight['dest_name']}?"
            ))
            st.session_state.stage = "offer_itinerary"
        else:
            responses.append(("text",
                "Would you like me to search for hotels? "
                "Just say **yes** to see options, or **skip** to move on to itinerary planning."
            ))

    # ═══════════════════════════════════════════════════════
    # STAGE: HOTELS SHOWN
    # ═══════════════════════════════════════════════════════
    elif stage == "hotels_shown":
        skip = check_no() or (use_llm and get_intent() == "skip") or "skip" in user_input.lower()

        if skip:
            flight = st.session_state.selected_flight
            responses.append(("text",
                f"Got it — skipping hotel booking. Would you like me to "
                f"**generate a day-by-day itinerary** for {flight['dest_name']}?"
            ))
            st.session_state.stage = "offer_itinerary"
        else:
            selection = get_selection()
            if selection and 1 <= selection <= len(st.session_state.hotels):
                hotel = st.session_state.hotels[selection - 1]
                st.session_state.selected_hotel = hotel
                hotel_ref = f"HTL-{random.randint(100000, 999999)}"
                st.session_state.hotel_booking_ref = hotel_ref

                responses.append(("text",
                    f"Great choice! **{hotel['name']}** — "
                    f"{hotel['stars']}★ in {hotel['area']}, "
                    f"{hotel['room_type']} for {hotel['nights']} nights.\n\n"
                    f"Here's your hotel payment summary:"
                ))
                responses.append(("html", render_hotel_payment_card(hotel, hotel_ref)))

                room_cost = hotel["price_per_night"] * hotel["nights"]
                taxes = int(room_cost * 0.13)
                service_fee = int(room_cost * 0.05)
                total = room_cost + taxes + service_fee

                responses.append(("text",
                    f"**Shall I proceed with this payment of ${total}.00 {hotel['currency']} "
                    f"from your Visa ending in 4242?**\n\n"
                    f"Say **\"Confirm\"** to proceed or **\"Cancel\"** to go back."
                ))
                st.session_state.stage = "hotel_payment_confirm"
            else:
                responses.append(("text",
                    "Which hotel would you like to book? Say **\"Book hotel 1\"**, "
                    "**\"Book hotel 2\"**, or **\"Book hotel 3\"**. Or say **\"skip\"** to move on."
                ))

    # ═══════════════════════════════════════════════════════
    # STAGE: HOTEL PAYMENT CONFIRM
    # ═══════════════════════════════════════════════════════
    elif stage == "hotel_payment_confirm":
        if check_yes():
            hotel = st.session_state.selected_hotel
            hotel_ref = st.session_state.hotel_booking_ref
            conf_id = ''.join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=8))
            st.session_state.hotel_confirmation_id = conf_id

            responses.append(("thinking", "Verifying 2FA session... ✓"))
            responses.append(("thinking", "Processing hotel payment via Stripe..."))
            responses.append(("thinking", f"Confirming reservation with {hotel['name']}..."))

            responses.append(("html", render_hotel_confirmation_card(hotel, hotel_ref, conf_id)))

            flight = st.session_state.selected_flight
            responses.append(("text",
                f"🎉 **Hotel booked!** A confirmation email with your reservation voucher "
                f"has been sent to your registered email address.\n\n"
                f"You're all set with **{flight['airline']}** flight and **{hotel['name']}** hotel. "
                f"Would you like me to **generate a day-by-day itinerary** for your trip to {flight['dest_name']}?"
            ))
            st.session_state.stage = "offer_itinerary"

        elif check_no():
            responses.append(("text",
                "Hotel payment cancelled — no charge has been made.\n\n"
                "Would you like to pick a different hotel, or skip to itinerary planning?"
            ))
            st.session_state.stage = "hotels_shown"
        else:
            responses.append(("text",
                "I need a clear confirmation before processing the payment. "
                "Please say **\"Confirm\"** to proceed or **\"Cancel\"** to go back."
            ))

    # ═══════════════════════════════════════════════════════
    # STAGE: OFFER ITINERARY
    # ═══════════════════════════════════════════════════════
    elif stage == "offer_itinerary":
        wants_it = check_yes() or (use_llm and get_intent() == "ask_itinerary")
        if not use_llm:
            wants_it = wants_it or wants_itinerary(user_input)

        if wants_it:
            flight = st.session_state.selected_flight
            dest = flight["dest_name"]
            responses.append(("thinking", f"Generating personalized itinerary for {dest}..."))
            responses.append(("text", f"Here's your suggested itinerary for **{dest}**:"))
            responses.append(("html", render_itinerary(dest)))
            responses.append(("text",
                "📋 You can adjust any part of this itinerary — just tell me what to add, "
                "remove, or change.\n\n"
                "Need anything else? I can help you find hotels, update your trip, "
                "or start planning another journey!"
            ))
            st.session_state.stage = "complete"
        elif check_no():
            responses.append(("text",
                "No worries! Your bookings are confirmed. "
                "If you need anything else — itinerary changes, "
                "or a new trip — just let me know!"
            ))
            st.session_state.stage = "complete"
        else:
            responses.append(("text",
                "Would you like me to create an itinerary for your trip? "
                "Just say **yes** or **no**."
            ))

    # ═══════════════════════════════════════════════════════
    # STAGE: COMPLETE
    # ═══════════════════════════════════════════════════════
    elif stage == "complete":
        intent = get_intent()

        # LLM-driven intent routing
        if use_llm and intent == "search_flight":
            st.session_state.stage = "greeting"
            st.session_state.flights = []
            st.session_state.selected_flight = None
            st.session_state.hotels = []
            st.session_state.selected_hotel = None
            return process_message(user_input)

        elif use_llm and intent in ("ask_hotel",):
            if st.session_state.selected_hotel:
                h = st.session_state.selected_hotel
                responses.append(("text",
                    f"You already have a hotel booked: **{h['name']}** ({h['stars']}★) in {h['area']} "
                    f"for {h['nights']} nights.\n\n"
                    f"Would you like to search for a different hotel, or is there anything else I can help with?"
                ))
            elif st.session_state.selected_flight:
                st.session_state.stage = "offer_hotel"
                return process_message("yes")
            else:
                responses.append(("text", "I'd love to help find a hotel! Let's search for flights first so I can coordinate your trip."))

        elif use_llm and intent == "cancel_booking":
            pnr = st.session_state.pnr or "N/A"
            if st.session_state.selected_flight:
                responses.append(("text",
                    f"I can help with that. For booking **{pnr}**:\n\n"
                    f"✅ This booking is currently within the **free cancellation window**. "
                    f"A full refund of **${st.session_state.selected_flight['price']}.00** "
                    f"would be issued to your Visa ending in 4242.\n\n"
                    f"Shall I proceed with the cancellation? The refund typically takes "
                    f"5–10 business days to appear on your statement."
                ))
            else:
                responses.append(("text", "You don't have any active bookings to cancel."))

        elif use_llm and intent == "check_status":
            if st.session_state.pnr:
                responses.append(("text",
                    f"📊 **Booking Status for {st.session_state.pnr}:**\n\n"
                    f"• Status: **Confirmed** ✅\n"
                    f"• Payment: **Completed** ✅\n"
                    f"• E-ticket: **Delivered** ✅\n\n"
                    f"Everything looks good! Need anything else?"
                ))
            else:
                responses.append(("text", "You don't have any active bookings yet. Want to search for flights?"))

        elif use_llm and intent == "new_search":
            st.session_state.stage = "greeting"
            st.session_state.flights = []
            st.session_state.selected_flight = None
            st.session_state.hotels = []
            st.session_state.selected_hotel = None
            responses.append(("text", "Let's plan another trip! Where would you like to go?"))

        elif use_llm and parsed.get("agent_reply"):
            responses.append(("text", parsed["agent_reply"]))

        # Rule-based fallback for complete stage
        else:
            text_lower = user_input.lower()
            if any(w in text_lower for w in ["hotel", "stay", "accommodation", "room"]):
                if st.session_state.selected_hotel:
                    h = st.session_state.selected_hotel
                    responses.append(("text",
                        f"You already have a hotel booked: **{h['name']}** ({h['stars']}★) in {h['area']} "
                        f"for {h['nights']} nights.\n\n"
                        f"Would you like to search for a different hotel, or is there anything else I can help with?"
                    ))
                elif st.session_state.selected_flight:
                    st.session_state.stage = "offer_hotel"
                    return process_message("yes")
                else:
                    responses.append(("text", "I'd love to help find a hotel! Could you tell me which city and dates you need?"))
            elif any(w in text_lower for w in ["new", "another", "different", "search", "flight", "book", "find"]):
                st.session_state.stage = "greeting"
                st.session_state.flights = []
                st.session_state.selected_flight = None
                st.session_state.hotels = []
                st.session_state.selected_hotel = None
                responses.append(("text", "Let's plan another trip! Where would you like to go?"))
            elif any(w in text_lower for w in ["cancel", "refund"]):
                pnr = st.session_state.pnr or "N/A"
                responses.append(("text",
                    f"I can help with that. For booking **{pnr}**:\n\n"
                    f"✅ This booking is currently within the **free cancellation window**. "
                    f"A full refund of **${st.session_state.selected_flight['price']}.00** "
                    f"would be issued to your Visa ending in 4242.\n\n"
                    f"Shall I proceed with the cancellation? The refund typically takes "
                    f"5–10 business days to appear on your statement."
                ))
            elif any(w in text_lower for w in ["status", "where", "refund status"]):
                responses.append(("text",
                    f"📊 **Booking Status for {st.session_state.pnr}:**\n\n"
                    f"• Status: **Confirmed** ✅\n"
                    f"• Payment: **Completed** ✅\n"
                    f"• E-ticket: **Delivered** ✅\n\n"
                    f"Everything looks good! Need anything else?"
                ))
            else:
                responses.append(("text",
                    "I'm here to help! I can:\n\n"
                    "• **Search for flights** — just tell me where and when\n"
                    "• **Check your booking status** — ask about your current trip\n"
                    "• **Plan an itinerary** — I'll create a day-by-day plan\n"
                    "• **Help with cancellations** — I'll handle the refund process\n\n"
                    "What would you like to do?"
                ))

    return responses


# ════════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ════════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("## ✈️ SkyAgent AI")
    st.markdown("*Your Intelligent Travel Companion*")

    st.markdown("---")

    st.markdown("### 👤 Traveler Profile")
    st.markdown("""
    **Ahmed Khan**
    📧 ahmed@email.com
    ✈️ Preferred: Window Seat, Halal Meals
    💳 Visa •••• 4242 (Citi Travel)
    """)

    st.markdown("---")

    st.markdown("### 📊 Quick Stats")
    col1, col2 = st.columns(2)
    with col1:
        st.metric("Trips", "12", "+2")
    with col2:
        st.metric("Saved", "$340")

    st.markdown("---")

    st.markdown("### 🎯 Demo Capabilities")
    st.markdown("""
    - ✅ Flight search & booking
    - ✅ Smart payment confirmation
    - ✅ Hotel recommendation & booking
    - ✅ Trip itinerary generation
    - ✅ Multi-currency detection
    - ✅ Cancellation & refunds
    - ✅ AI-powered conversation
    - 🔜 Real-time alerts
    """)

    st.markdown("---")

    # AI Engine status
    st.markdown("### 🤖 AI Engine")
    client = get_groq_client()
    if client:
        st.success("Connected · Llama 3.3 70B", icon="✅")
    else:
        st.warning("No API key — using rule-based mode", icon="⚠️")
        api_key_input = st.text_input(
            "Groq API Key",
            type="password",
            placeholder="gsk_...",
            help="Get a free key at console.groq.com",
        )
        if api_key_input:
            st.session_state.groq_api_key = api_key_input
            st.rerun()

    st.markdown("---")
    st.markdown(
        '<div style="text-align:center;color:#4a5568;font-size:12px;">'
        'SkyAgent AI · v1.1 Prototype<br>Confidential Demo</div>',
        unsafe_allow_html=True,
    )

    if st.button("🔄 Reset Conversation", use_container_width=True):
        st.session_state.messages = []
        st.session_state.stage = "greeting"
        st.session_state.trip = {}
        st.session_state.flights = []
        st.session_state.selected_flight = None
        st.session_state.booking_ref = None
        st.session_state.pnr = None
        st.session_state.hotels = []
        st.session_state.selected_hotel = None
        st.session_state.hotel_booking_ref = None
        st.session_state.hotel_confirmation_id = None
        st.session_state.greeted = False
        st.rerun()


# ════════════════════════════════════════════════════════════════════════════════
# MAIN CHAT INTERFACE
# ════════════════════════════════════════════════════════════════════════════════

# Header
st.markdown(
    '<div class="app-header">'
    '<div class="app-header-icon">✈️</div>'
    '<div class="app-header-title">SkyAgent AI</div>'
    '<div class="app-header-sub">Your AI-Powered Travel Agent · Flights · Hotels · Trip Planning</div>'
    '</div>',
    unsafe_allow_html=True,
)

# Initial greeting
if not st.session_state.greeted:
    greeting = (
        "Hello! 👋 I'm **SkyAgent**, your AI travel agent. "
        "I can search flights, book tickets, handle payments, and plan your entire trip — "
        "all right here in this conversation.\n\n"
        "**Try saying something like:**\n\n"
    )
    suggestions = (
        '<span class="suggestion-chip">Find me flights from Lahore to London next Friday under $800</span>'
        '<span class="suggestion-chip">I need to fly from Karachi to Dubai tomorrow</span>'
        '<span class="suggestion-chip">Book a flight to Istanbul in June</span>'
    )
    st.session_state.messages.append({
        "role": "assistant",
        "content": greeting,
        "html_extra": suggestions,
    })
    st.session_state.greeted = True

# Render message history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"], avatar="✈️" if msg["role"] == "assistant" else "🧑‍💼"):
        if msg.get("content"):
            st.markdown(msg["content"])
        if msg.get("html_extra"):
            st.markdown(msg["html_extra"], unsafe_allow_html=True)

# Handle user input
if prompt := st.chat_input("Tell me where you'd like to go..."):
    # Display user message
    with st.chat_message("user", avatar="🧑‍💼"):
        st.markdown(prompt)
    st.session_state.messages.append({"role": "user", "content": prompt})

    # Process and display response
    with st.chat_message("assistant", avatar="✈️"):
        responses = process_message(prompt)
        full_content = ""
        full_html = ""

        for rtype, rcontent in responses:
            if rtype == "thinking":
                with st.spinner(rcontent):
                    time.sleep(random.uniform(0.8, 1.8))
            elif rtype == "text":
                st.markdown(rcontent)
                full_content += rcontent + "\n\n"
            elif rtype == "html":
                st.markdown(rcontent, unsafe_allow_html=True)
                full_html += rcontent

    st.session_state.messages.append({
        "role": "assistant",
        "content": full_content.strip() if full_content.strip() else None,
        "html_extra": full_html if full_html else None,
    })

    st.rerun()