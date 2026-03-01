#!/usr/bin/env bash
# =============================================================================
# AI Newsletter Pipeline — Setup Script
# =============================================================================
# Installs Docker, n8n, and Python dependencies on Ubuntu or macOS.
# Usage: chmod +x setup.sh && ./setup.sh
# =============================================================================

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "\n${BLUE}==>${NC} $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Detect OS
# ---------------------------------------------------------------------------
detect_os() {
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if command -v apt-get &>/dev/null; then
            OS="ubuntu"
        elif command -v yum &>/dev/null; then
            OS="centos"
        else
            OS="linux"
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        OS="macos"
    else
        log_error "Unsupported OS: $OSTYPE"
        exit 1
    fi
    log_info "Detected OS: $OS"
}

# ---------------------------------------------------------------------------
# Install Docker
# ---------------------------------------------------------------------------
install_docker() {
    if command -v docker &>/dev/null; then
        log_info "Docker is already installed: $(docker --version)"
        return
    fi

    log_step "Installing Docker..."

    case "$OS" in
        ubuntu)
            sudo apt-get update -qq
            sudo apt-get install -y -qq \
                ca-certificates curl gnupg lsb-release

            # Add Docker's GPG key
            sudo install -m 0755 -d /etc/apt/keyrings
            curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
                sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
            sudo chmod a+r /etc/apt/keyrings/docker.gpg

            # Add Docker repository
            echo \
                "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
                https://download.docker.com/linux/ubuntu \
                $(lsb_release -cs) stable" | \
                sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

            sudo apt-get update -qq
            sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin

            # Allow current user to run Docker without sudo
            sudo usermod -aG docker "$USER" || true
            log_warn "You may need to log out and back in for Docker group membership to take effect."
            ;;

        macos)
            if command -v brew &>/dev/null; then
                brew install --cask docker
                log_info "Docker Desktop installed. Please open Docker Desktop to complete setup."
            else
                log_error "Homebrew is required on macOS. Install it from https://brew.sh"
                log_error "Then run: brew install --cask docker"
                exit 1
            fi
            ;;

        *)
            log_error "Automatic Docker installation not supported for $OS."
            log_error "Install Docker manually: https://docs.docker.com/get-docker/"
            exit 1
            ;;
    esac

    log_info "Docker installed successfully"
}

# ---------------------------------------------------------------------------
# Install Docker Compose (if not bundled)
# ---------------------------------------------------------------------------
install_docker_compose() {
    if docker compose version &>/dev/null 2>&1; then
        log_info "Docker Compose (plugin) is available"
        return
    fi

    if command -v docker-compose &>/dev/null; then
        log_info "docker-compose (standalone) is available"
        return
    fi

    log_step "Installing Docker Compose plugin..."
    sudo apt-get install -y -qq docker-compose-plugin 2>/dev/null || {
        log_warn "Could not install docker-compose-plugin via apt. Trying standalone..."
        COMPOSE_VERSION="v2.24.5"
        sudo curl -SL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-$(uname -s)-$(uname -m)" \
            -o /usr/local/bin/docker-compose
        sudo chmod +x /usr/local/bin/docker-compose
    }
}

# ---------------------------------------------------------------------------
# Create docker-compose.yml for n8n
# ---------------------------------------------------------------------------
create_docker_compose() {
    log_step "Creating docker-compose.yml for n8n..."

    # Load .env if it exists
    if [[ -f "$SCRIPT_DIR/.env" ]]; then
        source "$SCRIPT_DIR/.env"
    fi

    local N8N_PORT="${N8N_PORT:-5678}"
    local N8N_ENCRYPTION_KEY="${N8N_ENCRYPTION_KEY:-$(openssl rand -hex 32)}"

    cat > "$SCRIPT_DIR/docker-compose.yml" <<YAML
version: '3.8'

services:
  n8n:
    image: n8nio/n8n:latest
    container_name: ai-newsletter-n8n
    restart: unless-stopped
    ports:
      - "${N8N_PORT}:5678"
    environment:
      # Core n8n config
      - N8N_ENCRYPTION_KEY=${N8N_ENCRYPTION_KEY}
      - N8N_HOST=0.0.0.0
      - N8N_PORT=5678
      - N8N_PROTOCOL=http
      - GENERIC_TIMEZONE=UTC
      - TZ=UTC
      # Pass through API keys as env vars accessible in workflows via \$env
      - GEMINI_API_KEY=\${GEMINI_API_KEY:-}
      - BREVO_API_KEY=\${BREVO_API_KEY:-}
      - BREVO_SENDER_EMAIL=\${BREVO_SENDER_EMAIL:-}
      - BREVO_SENDER_NAME=\${BREVO_SENDER_NAME:-AI Newsletter}
      - BREVO_RECIPIENT_EMAILS=\${BREVO_RECIPIENT_EMAILS:-}
      - GOOGLE_SHEETS_ID=\${GOOGLE_SHEETS_ID:-}
    volumes:
      - n8n_data:/home/node/.n8n
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://localhost:5678/healthz || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 3

volumes:
  n8n_data:
    driver: local
YAML

    log_info "docker-compose.yml created at: $SCRIPT_DIR/docker-compose.yml"
}

# ---------------------------------------------------------------------------
# Install Python dependencies
# ---------------------------------------------------------------------------
install_python_deps() {
    log_step "Setting up Python environment..."

    if ! command -v python3 &>/dev/null; then
        log_warn "Python 3 not found. Installing..."
        case "$OS" in
            ubuntu) sudo apt-get install -y -qq python3 python3-pip python3-venv ;;
            macos)  brew install python3 ;;
        esac
    fi

    log_info "Python version: $(python3 --version)"

    # Create virtual environment
    if [[ ! -d "$SCRIPT_DIR/venv" ]]; then
        python3 -m venv "$SCRIPT_DIR/venv"
        log_info "Virtual environment created at: $SCRIPT_DIR/venv"
    fi

    # Install dependencies
    source "$SCRIPT_DIR/venv/bin/activate"
    pip install --quiet --upgrade pip
    pip install --quiet -r "$SCRIPT_DIR/requirements.txt"
    log_info "Python dependencies installed"
}

# ---------------------------------------------------------------------------
# Create .env from template if it doesn't exist
# ---------------------------------------------------------------------------
setup_env_file() {
    if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
        if [[ -f "$SCRIPT_DIR/.env.example" ]]; then
            cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
            log_warn ".env file created from template. Edit it with your credentials:"
            log_warn "  $SCRIPT_DIR/.env"
        fi
    else
        log_info ".env file already exists"
    fi
}

# ---------------------------------------------------------------------------
# Create required directories
# ---------------------------------------------------------------------------
create_directories() {
    mkdir -p "$SCRIPT_DIR/logs"
    mkdir -p "$SCRIPT_DIR/output"
    log_info "Created logs/ and output/ directories"
}

# ---------------------------------------------------------------------------
# Start n8n
# ---------------------------------------------------------------------------
start_n8n() {
    log_step "Starting n8n via Docker..."

    cd "$SCRIPT_DIR"

    if docker compose up -d 2>/dev/null; then
        log_info "n8n started with 'docker compose'"
    elif docker-compose up -d 2>/dev/null; then
        log_info "n8n started with 'docker-compose'"
    else
        log_error "Failed to start n8n. Is Docker running?"
        log_error "On macOS, make sure Docker Desktop is open."
        return 1
    fi

    local port="${N8N_PORT:-5678}"
    log_info "n8n is starting up..."
    log_info "Access n8n at: http://localhost:${port}"
}

# ---------------------------------------------------------------------------
# Print next steps
# ---------------------------------------------------------------------------
print_next_steps() {
    local port="${N8N_PORT:-5678}"

    echo ""
    echo -e "${GREEN}============================================================${NC}"
    echo -e "${GREEN}  Setup Complete!${NC}"
    echo -e "${GREEN}============================================================${NC}"
    echo ""
    echo "Next steps:"
    echo ""
    echo "  1. Edit your credentials:"
    echo "     nano $SCRIPT_DIR/.env"
    echo ""
    echo "  2. Open n8n in your browser:"
    echo "     http://localhost:${port}"
    echo ""
    echo "  3. Import the workflow files in n8n:"
    echo "     - n8n_workflow_collection.json  (RSS collection, every 4 hours)"
    echo "     - n8n_workflow_newsletter.json  (Newsletter generation, Monday 9 AM)"
    echo ""
    echo "  4. Configure Google Sheets credentials in n8n:"
    echo "     Settings > Credentials > New > Google Sheets"
    echo ""
    echo "  5. Activate the workflows in n8n"
    echo ""
    echo "  6. OR use the Python fallback:"
    echo "     source venv/bin/activate"
    echo "     python newsletter_pipeline.py --dry-run"
    echo ""
    echo "  7. Set up cron for the Python pipeline (optional):"
    echo "     crontab -e"
    echo "     0 9 * * 1 cd $SCRIPT_DIR && ./venv/bin/python newsletter_pipeline.py"
    echo ""
    echo "See README.md for detailed instructions."
    echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    echo -e "${BLUE}"
    echo "  ╔═══════════════════════════════════════════╗"
    echo "  ║    AI Newsletter Pipeline — Setup         ║"
    echo "  ╚═══════════════════════════════════════════╝"
    echo -e "${NC}"

    detect_os
    create_directories
    setup_env_file
    install_docker
    install_docker_compose
    create_docker_compose
    install_python_deps
    start_n8n
    print_next_steps
}

main "$@"
