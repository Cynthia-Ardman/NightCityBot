# NightCityBot — Google Drive Backup Setup Guide

This guide walks you through setting up automated Google Drive backups for NightCityBot's database.

---

## 1. Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Click **Select a project** → **New Project**.
3. Name it something like `NightCityBot-Backups` and click **Create**.
4. Make sure the new project is selected in the top bar.

## 2. Enable the Google Drive API

1. In the Google Cloud Console, go to **APIs & Services** → **Library**.
2. Search for **Google Drive API**.
3. Click on it and press **Enable**.

## 3. Create a Service Account

1. Go to **APIs & Services** → **Credentials**.
2. Click **+ CREATE CREDENTIALS** → **Service account**.
3. Name it `nightcitybot-backup` (or similar) and click **Create and Continue**.
4. Skip the optional role/access steps — click **Done**.
5. You'll see your new service account in the list. Click on it.
6. Go to the **Keys** tab.
7. Click **Add Key** → **Create new key** → select **JSON** → **Create**.
8. A `.json` file will download — this is your credentials file. **Keep it safe.**

## 4. Create a Google Drive Folder

1. Go to [Google Drive](https://drive.google.com/).
2. Create a new folder called `NightCityBot-Backups` (or any name you prefer).
3. Right-click the folder → **Share**.
4. Open the downloaded JSON credentials file and find the `"client_email"` field (e.g., `nightcitybot-backup@your-project.iam.gserviceaccount.com`).
5. Share the folder with that email address, giving it **Editor** access.
6. Copy the folder ID from the URL. When you open the folder, the URL looks like:
   ```
   https://drive.google.com/drive/folders/1ABC_xYz-123456789
   ```
   The folder ID is: `1ABC_xYz-123456789`

## 5. Set Environment Secrets

Set the following environment variables/secrets in your Replit project:

### `GDRIVE_SERVICE_ACCOUNT_JSON`
Paste the **entire contents** of the downloaded JSON credentials file as the value. It should look like:
```json
{
  "type": "service_account",
  "project_id": "your-project-id",
  "private_key_id": "...",
  "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
  "client_email": "nightcitybot-backup@your-project.iam.gserviceaccount.com",
  "client_id": "...",
  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
  "token_uri": "https://oauth2.googleapis.com/token",
  ...
}
```

### `GDRIVE_BACKUP_FOLDER_ID`
The folder ID from step 4 (e.g., `1ABC_xYz-123456789`).

### Optional Settings

| Variable | Default | Description |
|---|---|---|
| `BACKUP_RETENTION_DAYS` | `30` | Number of days to keep old backups before auto-deletion |
| `BACKUP_HOUR` | `4` | Hour (UTC, 0-23) for the automated daily backup |
| `BACKUP_MINUTE` | `0` | Minute (0-59) for the automated daily backup |

## 6. Install Dependencies

The following Python packages are required (already in `requirements.txt`):

```
google-api-python-client
google-auth
```

## 7. Verify the Setup

1. Start the bot.
2. Run `!backup_now` in Discord (requires Fixer role).
3. Check your Google Drive folder — you should see a timestamped `.json.gz` file.
4. Run `!backup_status` to confirm the backup was recorded.

## 8. Available Commands

| Command | Description |
|---|---|
| `!backup_now` | Trigger an immediate full backup to Google Drive |
| `!backup_status` | Show last backup time, size, and Drive link |
| `!restore_db` | List available backups on Google Drive |
| `!restore_db <id>` | Restore database from a specific backup (requires confirmation) |

All commands require the **Fixer** role.

## What Gets Backed Up

- **All database tables** — exported as read-only SELECT queries (no writes during export)
- **Balance backups** — local snapshot files from `backups/`
- **Character sheet backups** — local files from `sheet_backups/`
- **Rent audit logs** — local files from `rent_audits/`

Everything is bundled into a single compressed `.json.gz` file and uploaded to Google Drive.

## Backup Rotation

Old backups are automatically deleted from Google Drive after the retention period (default: 30 days). This happens after each successful backup.

## Troubleshooting

- **"GDRIVE_SERVICE_ACCOUNT_JSON environment variable is not set"** — Make sure you've added the full JSON credentials as a Replit secret.
- **"GDRIVE_BACKUP_FOLDER_ID environment variable is not set"** — Add the Google Drive folder ID as a Replit secret.
- **"403 Forbidden" errors** — Make sure you shared the Drive folder with the service account email (Editor access).
- **"Drive API has not been used in project"** — Enable the Google Drive API in Google Cloud Console.
