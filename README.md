# Summarify Pro

AI-powered document summarization tool — parse PDF/Word/URL and generate summaries, key points, and translations via Zhipu AI GLM-4-Flash.

## Features

- Upload & parse **PDF** and **Word (.docx)** documents
- Parse **web page URL** content
- **AI Summary** — concise summary of any document
- **AI Key Points** — extract bullet-point highlights
- **AI Translate** — translate to English / Chinese / Spanish / German / Japanese
- **User auth** — register & login with JWT
- **Free tier** — 3 uses per day; **Premium** — unlimited (.99/mo)
- **Copy & download** results as .txt
- Responsive SaaS-style UI (blue tech theme)

## Tech Stack

- **Frontend**: HTML + Tailwind CSS + JavaScript
- **Backend**: Python Flask
- **AI**: Zhipu AI GLM-4-Flash API
- **Database**: SQLite (users, usage tracking)

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /api/user/register | Register new user |
| POST | /api/user/login | Login and get JWT token |
| GET | /api/user/usage | Get remaining daily uses |
| POST | /api/parse/pdf | Upload and parse PDF |
| POST | /api/parse/word | Upload and parse Word doc |
| POST | /api/parse/url | Parse content from URL |
| POST | /api/ai/generate | AI summary / keypoints / translate |

## Quick Start

### 1. Clone & install

`ash
cd summarify-pro
pip install -r requirements.txt
`

### 2. Configure environment

`ash
cp .env.example .env
# Edit .env and fill in your keys:
#   ZHIPU_API_KEY=your_zhipu_api_key
#   SECRET_KEY=your_random_secret_key
#
# Get Zhipu API key: https://open.bigmodel.cn/usercenter/apikeys
`

### 3. Run

`ash
python app.py
# Server starts at http://localhost:5000
`

## Environment Variables (.env)

| Variable | Required | Description |
|----------|----------|-------------|
| ZHIPU_API_KEY | Yes | Zhipu AI API key (GLM-4-Flash) |
| SECRET_KEY | Yes | Flask JWT secret key (any random string) |

## Project Structure

`
summarify-pro/
├── app.py              # Flask backend
├── requirements.txt    # Python dependencies
├── .env.example       # Environment template
├── .gitignore         # Git ignore rules
└── static/
    ├── index.html      # Frontend page
    ├── script.js       # Frontend logic
    └── style.css      # Custom styles
`

## Free Tier Limits

- 3 AI operations per day (resets at midnight)
- Premium users: unlimited

## License

MIT