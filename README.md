# ✈️ SkyAgent AI — Prototype Demo

**AI-Powered Travel Agent** · Flight Booking · Trip Planning · Smart Payments

This is a working prototype for the client demo. It showcases the core conversational booking experience: natural language flight search → selection → payment confirmation → itinerary generation.

---

## Quick Start (2 minutes)

### 1. Install Python (if not already installed)
You need Python 3.9 or higher. Check with:
```bash
python --version
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Run the app
```bash
streamlit run app.py
```

The app will open automatically in your browser at `http://localhost:8501`.

---

## Demo Script (Suggested flow for client meeting)

### Step 1 — Flight Search
Type something like:
> "Find me flights from Lahore to London next Friday under $800"

The agent will parse your request and show 3 curated flight options with airline, times, stops, and prices.

### Step 2 — Select a Flight
Say:
> "Book option 2"

The agent will show a detailed payment summary card with itemized charges and the payment method.

### Step 3 — Confirm Payment
Say:
> "Confirm"

The agent processes the mock payment and shows a booking confirmation with PNR, transaction ID, and flight details.

### Step 4 — Hotel Recommendation
Agent asks if you want hotels. Say:
> "Yes"

The agent shows 3 hotel cards with star ratings, guest scores, amenities, prices, and cancellation policies.

### Step 5 — Book a Hotel
Say:
> "Book hotel 2"

The agent shows a hotel payment summary card. Say **"Confirm"** to complete.

### Step 6 — Get Itinerary
Say:
> "Yes"

The agent generates a day-by-day itinerary for the destination city.

### Bonus — Try These Too
- Ask about cancellation: "I want to cancel my booking"
- Check status: "What's the status of my booking?"
- Search again: "Find me flights to Dubai"
- Ask about hotels: "Can you find me a hotel?"

---

## What This Prototype Covers

| Feature                     | Status |
|-----------------------------|--------|
| Natural language flight search | ✅ Working |
| Multi-city support (40+ cities) | ✅ Working |
| Date parsing (next Friday, June 15, etc.) | ✅ Working |
| Budget filtering              | ✅ Working |
| Flight result cards            | ✅ Working |
| Payment confirmation flow      | ✅ Working |
| Booking confirmation with PNR  | ✅ Working |
| Hotel recommendation & booking | ✅ Working |
| Hotel payment flow             | ✅ Working |
| Trip itinerary generation      | ✅ Working |
| Cancellation flow (mock)       | ✅ Working |
| Booking status check           | ✅ Working |
| Real payment processing        | 🔜 Stripe integration |
| Real flight data               | 🔜 Duffel API |
| AI-powered conversation        | 🔜 LLM integration |

---

## Technical Notes

- **No API keys required** — all flight and hotel data is generated from realistic mock data
- **No cost** — runs entirely locally
- **No database** — state is in-memory (session-based)
- **Single file** — everything is in `app.py` for simplicity
- The conversation engine uses rule-based intent parsing. In the production version, this will be replaced with an LLM (Claude/GPT) with tool-calling capabilities.

## Hotels with Custom Data

London, Dubai, Paris, Istanbul, New York, Tokyo, and Singapore have hand-crafted hotel listings with real hotel names, accurate addresses, realistic pricing, and genuine amenities. All other cities use realistic default templates.

---

## Supported Cities (for demo)

Lahore, Karachi, Islamabad, London, New York, Dubai, Paris, Tokyo, Istanbul, Singapore, Bangkok, Toronto, Sydney, Jeddah, Riyadh, Doha, Mumbai, Delhi, Kuala Lumpur, Los Angeles, San Francisco, Chicago, Barcelona, Rome, Amsterdam, Frankfurt, Beijing, Hong Kong, Cairo, Abu Dhabi, Muscat, Colombo, Dhaka, Bali, Seattle, Boston, Washington D.C., Milan, Madrid, Berlin, Vienna, Zurich, and more.

---

*SkyAgent AI · v1.1 Prototype · Confidential*