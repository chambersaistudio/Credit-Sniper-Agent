#!/bin/bash
set -e

echo "🚀 Starting Credit Sniper Agent..."

# Check for .env
if [ ! -f .env ]; then
    echo "📋 Creating .env from template..."
    cp .env.example .env
    echo "⚠️  Edit .env and add your ANTHROPIC_API_KEY before continuing"
    exit 1
fi

# Start with Docker Compose
echo "🐳 Starting services with Docker Compose..."
docker compose up -d db redis

echo "⏳ Waiting for database..."
sleep 5

echo "🔧 Starting backend..."
docker compose up -d backend

echo "🎨 Starting frontend..."
docker compose up -d frontend

echo ""
echo "✅ Credit Sniper Agent is running!"
echo "   Backend API:  http://localhost:8000"
echo "   Frontend UI:  http://localhost:5173"
echo "   API Docs:     http://localhost:8000/docs"
echo ""
echo "📖 Phase 1 workflow:"
echo "   1. Set up your profile at http://localhost:5173/profile"
echo "   2. Upload a credit report at http://localhost:5173/upload"
echo "   3. Review AI analysis and start disputes"
echo "   4. Approve letters and send via certified mail"
echo "   5. Record bureau responses when received"
