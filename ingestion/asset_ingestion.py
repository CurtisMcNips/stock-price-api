# .github/workflows/asset-ingestion.yml
# ─────────────────────────────────────────────────────────────
# Runs the Market Brain asset ingestion bot on a schedule.
# Option C — GitHub Actions scheduler.
#
# Setup:
#   1. Add these secrets in GitHub → Settings → Secrets → Actions:
#      MB_API_URL      = https://your-railway-app.up.railway.app
#      MB_BOT_EMAIL    = your-bot@email.com
#      MB_BOT_PASSWORD = your-bot-password
#      ASSET_DB_PATH   = market_brain_assets.db  (or leave default)
#
#   2. Push this file to .github/workflows/asset-ingestion.yml
#   3. GitHub will run it automatically on the schedule below.
# ─────────────────────────────────────────────────────────────

name: Asset Ingestion Bot

on:
  schedule:
    # Daily update — 06:00 UTC every day
    - cron: '0 6 * * *'
    # Full run — 02:00 UTC every Sunday
    - cron: '0 2 * * 0'

  # Allow manual trigger from GitHub Actions tab
  workflow_dispatch:
    inputs:
      mode:
        description: 'Ingestion mode'
        required: true
        default: 'update'
        type: choice
        options:
          - update
          - full
          - crypto
          - status

jobs:
  ingest:
    runs-on: ubuntu-latest
    timeout-minutes: 30

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install dependencies
        run: |
          pip install -r ingestion/requirements-ingestion.txt

      - name: Restore asset database cache
        uses: actions/cache@v4
        with:
          path: ingestion/market_brain_assets.db
          key: asset-db-${{ github.run_number }}
          restore-keys: |
            asset-db-

      - name: Determine ingestion mode
        id: mode
        run: |
          # If manually triggered, use that mode
          if [ "${{ github.event_name }}" = "workflow_dispatch" ]; then
            echo "mode=${{ github.event.inputs.mode }}" >> $GITHUB_OUTPUT
          # Sunday 02:00 = full run
          elif [ "$(date +%u)" = "7" ] && [ "$(date +%H)" = "02" ]; then
            echo "mode=full" >> $GITHUB_OUTPUT
          else
            echo "mode=update" >> $GITHUB_OUTPUT
          fi

      - name: Run ingestion bot
        working-directory: ingestion
        env:
          MB_API_URL:      ${{ secrets.MB_API_URL }}
          MB_BOT_EMAIL:    ${{ secrets.MB_BOT_EMAIL }}
          MB_BOT_PASSWORD: ${{ secrets.MB_BOT_PASSWORD }}
          ASSET_DB_PATH:   market_brain_assets.db
          MIN_PRICE:       "0.50"
          MIN_ADV_USD:     "500000"
          SKIP_OTC:        "true"
          SKIP_PENNY:      "true"
          FETCH_CONCURRENCY: "5"
        run: |
          python ingestion_engine.py --mode ${{ steps.mode.outputs.mode }}

      - name: Print universe status
        working-directory: ingestion
        env:
          ASSET_DB_PATH: market_brain_assets.db
        run: |
          python ingestion_engine.py --mode status

      - name: Upload database artifact
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: asset-database-${{ github.run_number }}
          path: ingestion/market_brain_assets.db
          retention-days: 30

      - name: Notify on failure
        if: failure()
        run: |
          echo "::error::Ingestion bot failed. Check logs above."
