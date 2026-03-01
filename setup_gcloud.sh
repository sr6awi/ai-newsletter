#!/usr/bin/env bash
# =============================================================================
# Google Cloud Setup — Service Account + Google Sheets for AI Newsletter
# =============================================================================
# Prerequisites: gcloud CLI installed (https://cloud.google.com/sdk/docs/install)
#
# This script:
#   1. Creates a GCP project (or uses existing)
#   2. Enables Google Sheets API + Google Drive API
#   3. Creates a service account with a JSON key
#   4. Creates a Google Sheet with the correct headers
#   5. Shares the sheet with the service account
#   6. Updates your .env file with the Sheet ID
#
# Usage:
#   chmod +x setup_gcloud.sh
#   ./setup_gcloud.sh
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "\n${BLUE}==>${NC} $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SA_KEY_FILE="$SCRIPT_DIR/service_account.json"
ENV_FILE="$SCRIPT_DIR/.env"

PROJECT_ID=""
SA_NAME="newsletter-bot"
SA_DISPLAY="AI Newsletter Bot"

# ---------------------------------------------------------------------------
# Check prerequisites
# ---------------------------------------------------------------------------
check_gcloud() {
    if ! command -v gcloud &>/dev/null; then
        log_error "gcloud CLI not found."
        echo ""
        echo "Install it from: https://cloud.google.com/sdk/docs/install"
        echo ""
        echo "  macOS:   brew install --cask google-cloud-sdk"
        echo "  Ubuntu:  sudo snap install google-cloud-cli --classic"
        echo "  Manual:  curl https://sdk.cloud.google.com | bash"
        echo ""
        exit 1
    fi
    log_info "gcloud CLI found: $(gcloud version 2>/dev/null | head -1)"
}

# ---------------------------------------------------------------------------
# Authenticate
# ---------------------------------------------------------------------------
authenticate() {
    log_step "Checking Google Cloud authentication..."

    local account
    account=$(gcloud auth list --filter="status:ACTIVE" --format="value(account)" 2>/dev/null || true)

    if [[ -z "$account" ]]; then
        log_warn "Not logged in. Opening browser for authentication..."
        gcloud auth login --brief
        account=$(gcloud auth list --filter="status:ACTIVE" --format="value(account)" 2>/dev/null)
    fi

    log_info "Authenticated as: $account"
}

# ---------------------------------------------------------------------------
# Create or select project
# ---------------------------------------------------------------------------
setup_project() {
    log_step "Setting up GCP project..."

    local existing
    existing=$(gcloud config get-value project 2>/dev/null || true)

    if [[ -n "$existing" && "$existing" != "(unset)" ]]; then
        echo ""
        echo "  Current project: $existing"
        read -rp "  Use this project? [Y/n]: " use_existing
        if [[ "${use_existing,,}" != "n" ]]; then
            PROJECT_ID="$existing"
            log_info "Using existing project: $PROJECT_ID"
            return
        fi
    fi

    echo ""
    read -rp "  Enter a GCP project ID to create (e.g., ai-newsletter-12345): " PROJECT_ID

    if gcloud projects describe "$PROJECT_ID" &>/dev/null 2>&1; then
        log_info "Project '$PROJECT_ID' already exists — using it"
    else
        log_info "Creating project: $PROJECT_ID"
        gcloud projects create "$PROJECT_ID" --name="AI Newsletter" --set-as-default
    fi

    gcloud config set project "$PROJECT_ID"
    log_info "Active project: $PROJECT_ID"
}

# ---------------------------------------------------------------------------
# Enable APIs
# ---------------------------------------------------------------------------
enable_apis() {
    log_step "Enabling required APIs..."

    local apis=("sheets.googleapis.com" "drive.googleapis.com")

    for api in "${apis[@]}"; do
        log_info "Enabling $api..."
        gcloud services enable "$api" --project="$PROJECT_ID" 2>/dev/null || {
            log_warn "Could not enable $api — you may need to enable billing."
            log_warn "Note: Sheets & Drive APIs are free, but GCP requires a billing account."
            echo ""
            echo "  Enable manually at:"
            echo "  https://console.cloud.google.com/apis/library?project=$PROJECT_ID"
            echo ""
            read -rp "  Press Enter after enabling APIs to continue..."
        }
    done

    log_info "APIs enabled"
}

# ---------------------------------------------------------------------------
# Create service account + download key
# ---------------------------------------------------------------------------
create_service_account() {
    log_step "Creating service account..."

    local sa_email="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

    # Check if SA already exists
    if gcloud iam service-accounts describe "$sa_email" --project="$PROJECT_ID" &>/dev/null 2>&1; then
        log_info "Service account already exists: $sa_email"
    else
        gcloud iam service-accounts create "$SA_NAME" \
            --project="$PROJECT_ID" \
            --display-name="$SA_DISPLAY" \
            --description="Service account for AI Newsletter pipeline"
        log_info "Created service account: $sa_email"
    fi

    # Generate key file
    if [[ -f "$SA_KEY_FILE" ]]; then
        log_warn "Key file already exists at: $SA_KEY_FILE"
        read -rp "  Overwrite? [y/N]: " overwrite
        if [[ "${overwrite,,}" != "y" ]]; then
            log_info "Keeping existing key file"
            echo "$sa_email"
            return
        fi
    fi

    gcloud iam service-accounts keys create "$SA_KEY_FILE" \
        --iam-account="$sa_email" \
        --project="$PROJECT_ID"

    chmod 600 "$SA_KEY_FILE"
    log_info "Key downloaded to: $SA_KEY_FILE"
    echo "$sa_email"
}

# ---------------------------------------------------------------------------
# Create Google Sheet via Drive API
# ---------------------------------------------------------------------------
create_google_sheet() {
    local sa_email="$1"

    log_step "Creating Google Sheet..."

    # Get an access token for the current user
    local token
    token=$(gcloud auth print-access-token)

    # Create spreadsheet via Sheets API
    local create_response
    create_response=$(curl -s -X POST \
        "https://sheets.googleapis.com/v4/spreadsheets" \
        -H "Authorization: Bearer $token" \
        -H "Content-Type: application/json" \
        -d '{
            "properties": {
                "title": "AI Newsletter Articles"
            },
            "sheets": [{
                "properties": {
                    "title": "Articles",
                    "gridProperties": {
                        "frozenRowCount": 1
                    }
                },
                "data": [{
                    "startRow": 0,
                    "startColumn": 0,
                    "rowData": [{
                        "values": [
                            {"userEnteredValue": {"stringValue": "hash"}},
                            {"userEnteredValue": {"stringValue": "title"}},
                            {"userEnteredValue": {"stringValue": "url"}},
                            {"userEnteredValue": {"stringValue": "date"}},
                            {"userEnteredValue": {"stringValue": "source"}},
                            {"userEnteredValue": {"stringValue": "content_snippet"}},
                            {"userEnteredValue": {"stringValue": "category"}},
                            {"userEnteredValue": {"stringValue": "relevance_score"}},
                            {"userEnteredValue": {"stringValue": "summary"}},
                            {"userEnteredValue": {"stringValue": "processed"}},
                            {"userEnteredValue": {"stringValue": "collected_at"}},
                            {"userEnteredValue": {"stringValue": "processed_at"}}
                        ]
                    }]
                }]
            }]
        }')

    local sheet_id
    sheet_id=$(echo "$create_response" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('spreadsheetId', ''))
" 2>/dev/null || true)

    if [[ -z "$sheet_id" ]]; then
        log_error "Failed to create spreadsheet."
        log_error "Response: $create_response"
        echo ""
        log_warn "Creating the sheet manually instead..."
        echo "  1. Go to https://sheets.google.com"
        echo "  2. Create a new spreadsheet named 'AI Newsletter Articles'"
        echo "  3. Rename the first tab to 'Articles'"
        echo "  4. Add headers: hash, title, url, date, source, content_snippet,"
        echo "     category, relevance_score, summary, processed, collected_at, processed_at"
        echo ""
        read -rp "  Enter the spreadsheet ID from the URL: " sheet_id

        if [[ -z "$sheet_id" ]]; then
            log_error "No sheet ID provided — skipping sheet setup"
            return
        fi
    else
        log_info "Spreadsheet created: $sheet_id"
        log_info "URL: https://docs.google.com/spreadsheets/d/$sheet_id/edit"
    fi

    # Share the sheet with the service account
    log_info "Sharing sheet with service account: $sa_email"
    curl -s -X POST \
        "https://www.googleapis.com/drive/v3/files/$sheet_id/permissions" \
        -H "Authorization: Bearer $token" \
        -H "Content-Type: application/json" \
        -d "{
            \"type\": \"user\",
            \"role\": \"writer\",
            \"emailAddress\": \"$sa_email\"
        }" > /dev/null 2>&1 && {
        log_info "Sheet shared with service account"
    } || {
        log_warn "Could not auto-share. Manually share the sheet with: $sa_email"
    }

    # Update .env file
    if [[ -f "$ENV_FILE" ]]; then
        if grep -q "^GOOGLE_SHEETS_ID=" "$ENV_FILE"; then
            sed -i.bak "s|^GOOGLE_SHEETS_ID=.*|GOOGLE_SHEETS_ID=$sheet_id|" "$ENV_FILE"
            rm -f "$ENV_FILE.bak"
        else
            echo "GOOGLE_SHEETS_ID=$sheet_id" >> "$ENV_FILE"
        fi
        log_info "Updated .env with GOOGLE_SHEETS_ID"
    fi

    echo ""
    echo -e "  ${GREEN}Sheet ID:${NC} $sheet_id"
    echo -e "  ${GREEN}URL:${NC} https://docs.google.com/spreadsheets/d/$sheet_id/edit"
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print_summary() {
    local sa_email="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

    echo ""
    echo -e "${GREEN}============================================================${NC}"
    echo -e "${GREEN}  Google Cloud Setup Complete!${NC}"
    echo -e "${GREEN}============================================================${NC}"
    echo ""
    echo "  Project:          $PROJECT_ID"
    echo "  Service Account:  $sa_email"
    echo "  Key File:         $SA_KEY_FILE"
    echo ""
    echo "  APIs enabled:"
    echo "    - Google Sheets API"
    echo "    - Google Drive API"
    echo ""
    echo "  Files updated:"
    echo "    - $SA_KEY_FILE (service account key)"
    echo "    - $ENV_FILE (GOOGLE_SHEETS_ID)"
    echo ""
    echo "  Remaining .env values to fill in:"
    echo "    - GEMINI_API_KEY     (from https://aistudio.google.com/apikey)"
    echo "    - BREVO_API_KEY      (from https://app.brevo.com/settings/keys/api)"
    echo "    - BREVO_SENDER_EMAIL (your verified sender in Brevo)"
    echo "    - BREVO_RECIPIENT_EMAILS (comma-separated recipients)"
    echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    echo -e "${BLUE}"
    echo "  ╔═══════════════════════════════════════════════╗"
    echo "  ║  Google Cloud Setup — AI Newsletter Pipeline  ║"
    echo "  ╚═══════════════════════════════════════════════╝"
    echo -e "${NC}"

    check_gcloud
    authenticate
    setup_project
    enable_apis
    local sa_email
    sa_email=$(create_service_account)
    create_google_sheet "$sa_email"
    print_summary
}

main "$@"
