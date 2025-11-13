# OnPage SEO – Backend API (FastAPI)

## Endpoints
- `GET /health` → `{ ok: true, ts: <unix> }`
- `POST /analyze-page` → يشغّل السلسلة (fetch → extract → analyze) ويرجع تقرير JSON واحد.

### Request
```json
{
  "url": "https://example.com/page/",
  "keyword": "focus keyword optional"
}
