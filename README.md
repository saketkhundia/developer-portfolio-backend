# Developer Portfolio Backend

FastAPI backend for the DevIQ developer portfolio application.

## Features

- OAuth2 authentication (Google & GitHub)
- MongoDB user storage
- User profile management
- GitHub integration for portfolio data
- Analytics tracking

## Environment Variables

Create a `.env` file with the following variables:

```
# OAuth Providers
GOOGLE_CLIENT_ID=your_client_id
GOOGLE_CLIENT_SECRET=your_client_secret
GITHUB_CLIENT_ID=your_client_id
GITHUB_CLIENT_SECRET=your_client_secret

# MongoDB
MONGODB_URI=mongodb+srv://...

# API
API_PORT=8000
```

## Installation

1. Install Python 3.8+
2. Create virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Running Locally

```bash
python main.py
```

Server will start on `http://localhost:8000`

## Deployment

### Render

1. Push code to GitHub
2. Connect repository to Render
3. Set environment variables in Render dashboard
4. Deploy

### Docker

```bash
docker build -t deviq-backend .
docker run -p 8000:8000 deviq-backend
```

## API Endpoints

- `POST /auth/oauth` - OAuth code exchange
- `GET /user/{user_id}` - Get user profile
- `POST /user/profile` - Update user profile
- `GET /analytics` - Get analytics data

## License

MIT
