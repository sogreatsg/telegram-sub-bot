# 🤖 Project Guidelines & Workflow Rules

## 1. 🔍 Mandatory Code Verification Before Commit
* **Always run pre-commit verification:** Before staging and committing any Python code, run an import & syntax check using the local environment:
  ```bash
  .\venv\Scripts\python.exe -c "from bot.handlers import user_menu, payment, admin, channel_events; from bot.main import main; print('OK')"
  ```
* **Verify tests:** When modifying business logic (e.g., subscriptions, referrals, reconciliation), run the corresponding test script in `scripts/`.
* **Zero Runtime Crashes:** Ensure all imports, type annotations, and database schema mappings are 100% valid.

## 2. 🚀 Automated Git Commit & Push
* **Auto-Commit & Push:** Once code changes are thoroughly verified and tests pass, automatically stage, commit (using descriptive commit messages), and push to `origin main`.
* **PowerShell Compatibility:** Use `;` instead of `&&` when chaining git commands on Windows PowerShell.

## 3. ☁️ Production Deployment Awareness (GCP GCE)
* **Live Deployment:** Be aware that pushes to `main` immediately deploy to the live production VM on GCP via GitHub Actions.
* **No Per-Deploy DB Backups:** Do not execute database backup scripts inside deployment workflows (database backups are handled via daily cron).

## 4. 👑 Admin Commands Synchronization (`/admin` Menu)
* **Always Update `/admin` Menu:** Whenever adding, renaming, or updating an admin command in `bot/handlers/admin.py`:
  - Synchronize the command description in `handle_admin_menu_command` (`admin_menu_text`).
  - If the command is a primary administrative tool, consider adding a quick-access Inline Keyboard button.
  - Ensure syntax examples and descriptions in the `/admin` menu are 100% accurate.

## 5. 🔇 Communication Style & Feedback Rules
* **No Evaluation or Feedback Questions:** Do not end responses with evaluation, rating, or confirmation questions (e.g., "ดีไหมครับ", "ถูกใจไหมครับ", "มีอะไรให้ปรับไหมครับ", "feedback").
* **Direct & Concise Delivery:** Provide answers, technical analysis, and code implementations directly and concisely without asking follow-up evaluation questions unless strictly necessary for requirements.
