# HealthLab AI - Unified API Documentation

This backend serves both the Web interface and eventually the Mobile application.

### Current Structure:
- **Web Routes**: Handled via `render_template` in `app.py`.
- **API Routes (Planned)**: Will be added under the `/api/v1/` prefix.

### Sharing Data:
Both Frontends (Web & Mobile) share the **same MongoDB database**:
- Database: `digital_healthcare`
- Collections: `users`, `bookings`, `reports`

### How to add Mobile Support:
For every feature we build on the web, we can add a corresponding JSON route.
Example:
- Web: `GET /dashboard` -> returns `dashboard.html`
- Mobile: `GET /api/dashboard` -> returns `{"user": "...", "recent_reports": [...]}`
