# Telecom Daily Reporting Bot

A Python Telegram bot built to automate daily reporting for telecom field crews.

## Features
- Crew leader registration
- Worker selection
- Site name entry
- Completed work tracking
- Issue reporting
- Photo uploads
- Plan for tomorrow
- Automatic report generation
- SQLite data storage
- Scheduled reminders
- Timezone support
- Worker statistics

## Tech Stack
- Python
- python-telegram-bot
- Telegram Bot API
- SQLite
- JobQueue
- python-dotenv
- zoneinfo

## How It Works

The bot guides crew leaders through a step-by-step daily reporting process.

A typical report includes:
1. Crew members
2. Site name
3. Work completed
4. Job-site photos
5. Issues or delays
6. Plan for the next day

The completed report is sent to a configured Telegram group.

## Setup

Install dependencies:

```bash
pip install -r requirements.txt
