#!/bin/bash
set -e

echo "🚀 Starting Credit Sniper Agent..."

# Check for .env
if [ ! -f .env ]; then
    echo "📋 Creating .env from template..."
    cp .env.example .env
    echo "⚠️  Edit .env and add your ANTHROPIC_API_KEY (or run `ant auth login`) before continuing"
    exit 1
fi

# Start with Docker Compose
echo "🐳 Starting services with Docker Compose..."
docker compose up -d db

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
echo "📖 Workflow:"
echo "   1. Upload each bureau's report (Reports tab)"
echo "   2. Review accounts and findings; evaluate the ones you want checked"
echo "   3. Open a case where a dispute ground exists, review the package, approve"
echo "   4. Send it, record when, and record the response when it arrives"
