"""Railway entrypoint shared by both services (railway.json can hold only one start command).

Dispatches on RAILWAY_SERVICE_NAME, which Railway injects:
  sync -> run the nightly sync once and exit (schedule it with the service's Cron Schedule)
  anything else -> serve the dashboard
"""
import os
import sys

role = os.environ.get("SERVICE_ROLE") or os.environ.get("RAILWAY_SERVICE_NAME", "web")

if role == "sync":
    from sync.run_sync import main
    main()
    sys.exit(0)

port = os.environ.get("PORT", "8000")
os.execvp(sys.executable, [sys.executable, "-m", "uvicorn", "app.main:app",
                           "--host", "0.0.0.0", "--port", port])
