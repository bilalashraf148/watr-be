# watr-be

Django + DRF API for WATR (weather-aware truck routing): 3 truck routes, weather at each checkpoint's
ETA, risk per the assessment rules, a recommended route and a 0–48 h risk heatmap. Stateless.
Frontend: `../watr-fe`.

## Requirements

- Python 3.10+
- TomTom API key (free: <https://developer.tomtom.com>)

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env    # set DJANGO_SECRET_KEY and TOMTOM_API_KEY
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver 8000
```

Secret key: `.venv/bin/python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"`
Frontend: `cd ../watr-fe && npm install && npm run dev` → <http://localhost:5173>

## Test & lint

```bash
.venv/bin/python -m pytest      # mocks all HTTP calls
.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check .
```

## Environment

| Variable | Default | Note |
|---|---|---|
| `DJANGO_SECRET_KEY` | required | |
| `TOMTOM_API_KEY` | required | routing + place search |
| `DJANGO_DEBUG` | `false` | |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | |
| `DATABASE_URL` | SQLite | e.g. `postgres://user:pass@host:5432/db` |
| `HTTP_TIMEOUT_SECONDS` | `10` | third-party calls |
| `DJANGO_LOG_LEVEL` | `INFO` | |

## Third parties

| Service | Use | Key | Free tier |
|---|---|---|---|
| TomTom Routing API | truck routes, travel times | yes | 2,500 requests/day |
| TomTom Search API | place suggestions (global) | yes | shared with routing |
| Open-Meteo | hourly wind, gusts, rain, snow | no | 600 locations/min, 10,000/day, non-commercial, CC BY 4.0 |

Libraries: Django 5.2, Django REST Framework, django-environ, httpx, psycopg. Dev: pytest, pytest-django, ruff.

## API

- `GET /api/v1/places/?q=chicago` → `{"results": [{"label", "lat", "lon"}]}`
- `POST /api/v1/trips/plan/`

```json
{
  "origin": {"label": "Chicago, IL", "lat": 41.88, "lon": -87.63},
  "destination": {"label": "Denver, CO", "lat": 39.74, "lon": -104.99},
  "departure_time": "2026-11-26T08:00:00-06:00",
  "load_weight_lb": 42000,
  "checkpoint_interval_miles": 25
}
```

Returns ranked `routes` (distance, duration, arrival, `risk_summary`, risk-coloured `segments`,
`checkpoints` with ETA, weather and risk), `recommended_route_id`, `recommendation_reason`,
`no_safe_route` and `heatmap` (49 hourly scores per grid cell). Full shape: `trips/serializers.py`.

Errors: 400 invalid input · 422 no route, trip > 3,000 mi or forecast gap · 429 rate limit (places
60/min, plans 10/min per IP) · 502 TomTom / Open-Meteo unavailable.

## How it works

`TripPlanView` → `services.trip_plan`:

1. **Routes**: TomTom native truck routing with 2 alternatives (exactly 3 routes total); no detour fallback.
2. **Checkpoints**: every 10 / 25 / 50 mi plus the destination; ETA from TomTom's turn-by-turn timing.
3. **Weather**: one Open-Meteo fetch for a ≤ 150-cell grid along the corridor, cached 15 min. Wind
   at the nearest hour; rain and snow from the hour containing the ETA.
4. **Risk** (`risk.classify`): worst of wind, rain, snow; load rules raise wind risk.
5. **Ranking** (`recommendation.rank`): No Travel miles → Severe miles → High miles → average risk →
   travel time. A route with No Travel miles is never recommended.
6. **Heatmap**: same rules per grid cell for hours 0–48 from departure.

| | Low | Moderate | High | Severe | No Travel |
|---|---|---|---|---|---|
| Wind, mph (sustained) | < 25 | 25–<35 | 35–<45 | 45–<55 | ≥ 55 |
| Rain, in/hr | < 0.10 | 0.10–<0.25 | 0.25–<0.50 | 0.50–1.00 | > 1.00 |
| Snow, in/hr | < 0.5 | 0.5–<1.0 | 1.0–<2.0 | 2.0–3.0 | > 3.0 |

Load: wind 45–<55 mph and > 30,000 lb → No Travel; wind 35–<45 mph and > 40,000 lb → Severe.

Code: `trips/clients/` (HTTP) · `geo.py`, `risk.py`, `weather.py`, `recommendation.py` (pure logic) ·
`services.py` (orchestration) · `serializers.py`, `views.py` · `tests/`.

## Limits

- No driver rest breaks; load weight affects risk, not route choice.
- Caches and rate limits are per process: use a shared cache (e.g. Redis) with several workers. Behind
  a reverse proxy, set `NUM_PROXIES` in `config/settings.py`.
- Open-Meteo free tier allows about 4 uncached plans per minute.

## Deployment

- **Backend**: Railway (auto-deploys on push to `master`)
  - Create PostgreSQL add-on in same project
  - Set env vars: `DATABASE_URL`, `TOMTOM_API_KEY`, `DJANGO_SECRET_KEY`, `CORS_ALLOWED_ORIGINS` (Vercel domain), `DJANGO_DEBUG=false`
- **Frontend**: Vercel (auto-deploys on push to `master`)
  - Set `VITE_API_BASE_URL` to Railway backend URL (e.g., `https://watr-be-production.up.railway.app/api/v1`)
