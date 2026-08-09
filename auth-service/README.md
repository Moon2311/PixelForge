# auth-service

Authentication microservice for PixelForge — Django REST Framework backed by PostgreSQL. Manages user registration, login, password reset, and issues shared signed access tokens consumed by the search-service.

## Architecture

```
Client (Browser)
    │
    │  /api/auth/*
    ▼
nginx (:80) ──► auth-service (:8001) ──► PostgreSQL (:5432)
                                            DB: pixelforge
                                            Tables: auth_user, auth_token,
                                                    apps_role, apps_userprofile
```

### Data stores

| Store | Purpose |
|-------|---------|
| PostgreSQL (`pixelforge`) | Users, roles, profiles, password reset tokens |

### Key components

| Component | Path | Purpose |
|-----------|------|---------|
| **models.py** | `apps/models.py` | `Role` (admin, buyer, inventory_manager) and `UserProfile` (OneToOne with Django `User`, FK to `Role`) |
| **serializers.py** | `apps/serializers.py` | `BuyerRegistrationSerializer`, `InventoryManagerCreateSerializer`, `LoginSerializer`, `ForgotPasswordSerializer`, `ResetPasswordSerializer`, `UserSerializer` |
| **views.py** | `apps/views.py` | `RegisterBuyerView`, `CreateInventoryManagerView`, `LoginView`, `ForgotPasswordView`, `ResetPasswordView` |
| **responses.py** | `apps/responses.py` | `APIResponse` static helpers (`success`, `created`, `error`, `bad_request`, `not_found`, `forbidden`, `server_error`) returning uniform `{message, status_code, data}` |
| **auth_tokens.py** | `apps/auth_tokens.py` | `issue_access_token()` — signs a payload `id:role:is_staff` with `TimestampSigner` (8h TTL, salt `pixelforge.access`) |
| **urls.py** | `apps/urls.py` | Routes: `register/buyer/`, `create/inventory-manager/`, `login/`, `forgot-password/`, `reset-password/` |
| **urls.py (config)** | `config/urls.py` | Root URLconf — includes `apps.urls` under `/api/auth/` |
| **settings.py** | `config/settings.py` | PostgreSQL config, CORS (open), `SHARED_AUTH_SECRET`, `PASSWORD_RESET_TIMEOUT=3600` |
| **admin.py** | `apps/admin.py` | Custom `UserAdmin` with `UserProfileInline`, registers `Role` |
| **seed_users.py** | `apps/management/commands/seed_users.py` | Seeds 3 roles + 3 demo users |
| **Dockerfile** | `Dockerfile` | Python 3.10-slim, gunicorn on `:8000` |

### Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/auth/register/buyer/` | Public | Register a buyer account (username, email, password) |
| POST | `/api/auth/create/inventory-manager/` | Admin | Create an inventory manager (`is_staff=True`) |
| POST | `/api/auth/login/` | Public | Login with username **or** email; returns `{user, access_token}` |
| POST | `/api/auth/forgot-password/` | Public | Generate a password-reset token; returns `{user_id, token}` |
| POST | `/api/auth/reset-password/` | Public | Reset password using `user_id` + `token`; restricted to buyers |

### Authentication flow

1. **Login** (`POST /api/auth/login/`): Authenticates by `username` OR `email` via Django `Q` lookup. On success, calls `issue_access_token(user)` which signs `{user_id}:{role_name}:{is_staff}` with `SHARED_AUTH_SECRET` using `TimestampSigner` (8h TTL). Returns the signed token as `access_token`.

2. **Token verification** (search-service): The search-service uses `SharedTokenAuthentication` which:
   - Reads the `Authorization: Bearer <token>` header
   - Calls `TimestampSigner.unsign(token, max_age=8h)` with the same `SHARED_AUTH_SECRET` and salt
   - Parses the payload `id:role:is_staff` into an `AuthUser` object
   - Attaches `AuthUser` to `request.user`

3. **Authorization** (search-service): The `IsAdmin` permission class checks `request.user.role == "admin"`. Non-admin tokens receive 403; missing/invalid tokens receive 401.

### Response format

All responses use the uniform `APIResponse` envelope:

```json
{ "message": "...", "status_code": 200, "data": { ... } }
```

### Demo users

| Username | Email | Password | Role |
|----------|-------|----------|------|
| `admin` | admin@pixelforge.com | `admin123` | admin |
| `buyer` | buyer@pixelforge.com | `buyer123` | buyer |
| `inventory_manager` | inventory@pixelforge.com | `inventory123` | inventory_manager |

### Setup (local dev)

```bash
cd PixelForgebackend/auth-service
source ../.venv/bin/activate
pip install -r ../requirements.txt

createdb pixelforge
python manage.py migrate
python manage.py seed_users
python manage.py runserver 0.0.0.0:8001
```

- DB: PostgreSQL on `localhost:5432`, user `postgres`, password `3654`, database `pixelforge`
- CORS: open for all origins (local dev only)
- `SHARED_AUTH_SECRET` must match the search-service value for cross-service auth

### Docker

```bash
cd PixelForgebackend
docker compose up --build
```

`auth-service` is exposed on host port `8001` (gunicorn binds `:8000` inside the container).