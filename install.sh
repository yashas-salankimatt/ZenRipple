#!/bin/bash
# Zen AI Agent Installer
# Usage: ./install.sh [OPTIONS]
#
# Installs the Zen AI Agent browser automation server into a Zen Browser profile.
# Requires fx-autoconfig to be installed (comes with Zen Browser / ZenLeap).
#
# Options:
#   --profile <index>   Select profile by index (1-based); omit for interactive selection
#   --yes, -y           Auto-confirm all prompts (non-interactive mode)
#   --uninstall         Remove the agent from the selected profile
#   --list              Show installed status for each profile
#   --help, -h          Show this help
#
# Examples:
#   ./install.sh
#   ./install.sh --profile 1 --yes
#   ./install.sh --uninstall --profile 1

set -e

# Colors
RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
BLUE=$'\033[0;34m'
CYAN=$'\033[0;36m'
DIM=$'\033[2m'
NC=$'\033[0m'

# Pre-scan for non-interactive flags
_NON_INTERACTIVE=false
for _arg in "$@"; do
    case "$_arg" in
        --yes|-y) _NON_INTERACTIVE=true ;;
        --help|-h) _NON_INTERACTIVE=true ;;
        --list) _NON_INTERACTIVE=true ;;
    esac
done

# Open /dev/tty for interactive input
if [ "$_NON_INTERACTIVE" = true ]; then
    exec 3</dev/null
elif [ -t 0 ]; then
    exec 3<&0
else
    if [ -e /dev/tty ]; then
        exec 3</dev/tty
    else
        echo "Error: No terminal available. Use --yes and --profile for non-interactive mode."
        exit 1
    fi
fi

# Flags
PROFILE_INDEX=""
AUTO_YES=false
UNINSTALL_MODE=false
LIST_MODE=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Get version from zenleap_agent.uc.js
get_version() {
    local file="$1"
    if [ -f "$file" ]; then
        grep -o '@version[[:space:]]*[0-9.]*' "$file" | head -1 | sed 's/@version[[:space:]]*//'
    else
        echo ""
    fi
}

show_banner() {
    echo -e "${BLUE}"
    echo "======================================================"
    echo "           Zen AI Agent Installer"
    echo "   Browser Automation for Claude Code & AI Agents"
    echo "======================================================"
    echo -e "${NC}"
}

detect_os() {
    case "$(uname -s)" in
        Darwin)
            OS="macos"
            PROFILE_BASE="$HOME/Library/Application Support/zen/Profiles"
            ;;
        Linux)
            OS="linux"
            PROFILE_BASE="$HOME/.zen"
            ;;
        MINGW*|MSYS*|CYGWIN*)
            echo -e "${RED}Windows is not currently supported.${NC}"
            exit 1
            ;;
        *)
            echo -e "${RED}Unsupported operating system${NC}"
            exit 1
            ;;
    esac
    echo -e "${GREEN}+${NC} Detected OS: $OS"
}

find_profiles() {
    if [ ! -d "$PROFILE_BASE" ]; then
        echo -e "${RED}Error: Zen profile directory not found at $PROFILE_BASE${NC}"
        echo "Please run Zen Browser at least once to create a profile."
        exit 1
    fi

    PROFILES=()
    while IFS= read -r -d '' dir; do
        PROFILES+=("$dir")
    done < <(find "$PROFILE_BASE" -maxdepth 1 -type d ! -name "Profiles" ! -path "$PROFILE_BASE" -print0 2>/dev/null)

    if [ ${#PROFILES[@]} -eq 0 ]; then
        echo -e "${RED}Error: No Zen profiles found${NC}"
        exit 1
    fi
}

select_profiles() {
    # Build the list of profiles to operate on
    SELECTED_PROFILES=()

    if [ -n "$PROFILE_INDEX" ]; then
        # --profile flag: use specified profile index only
        if ! [[ "$PROFILE_INDEX" =~ ^[0-9]+$ ]] || [ "$PROFILE_INDEX" -lt 1 ] || [ "$PROFILE_INDEX" -gt ${#PROFILES[@]} ]; then
            echo -e "${RED}Error: Invalid profile index $PROFILE_INDEX (valid: 1-${#PROFILES[@]})${NC}"
            exit 1
        fi
        SELECTED_PROFILES+=("${PROFILES[$((PROFILE_INDEX-1))]}")
        echo -e "${GREEN}+${NC} Selected profile: $(basename "${SELECTED_PROFILES[0]}")"
    elif [ "$AUTO_YES" = true ]; then
        # --yes without --profile: install to ALL profiles (matches ZenLeap reference installer)
        SELECTED_PROFILES=("${PROFILES[@]}")
        if [ ${#SELECTED_PROFILES[@]} -eq 1 ]; then
            echo -e "${GREEN}+${NC} Found profile: $(basename "${SELECTED_PROFILES[0]}")"
        else
            echo -e "${GREEN}+${NC} Found ${#SELECTED_PROFILES[@]} profiles (installing to all):"
            for p in "${SELECTED_PROFILES[@]}"; do
                local pname
                pname=$(basename "$p")
                local status=""
                if [ -f "$p/chrome/JS/zenleap_agent.uc.js" ]; then
                    local v
                    v=$(get_version "$p/chrome/JS/zenleap_agent.uc.js")
                    status=" ${GREEN}(agent v$v installed)${NC}"
                fi
                echo -e "    - $pname$status"
            done
        fi
    elif [ ${#PROFILES[@]} -eq 1 ]; then
        SELECTED_PROFILES=("${PROFILES[0]}")
        echo -e "${GREEN}+${NC} Found profile: $(basename "${SELECTED_PROFILES[0]}")"
    else
        echo -e "${YELLOW}Multiple profiles found:${NC}"
        for i in "${!PROFILES[@]}"; do
            local pname
            pname=$(basename "${PROFILES[$i]}")
            local status=""
            if [ -f "${PROFILES[$i]}/chrome/JS/zenleap_agent.uc.js" ]; then
                local v
                v=$(get_version "${PROFILES[$i]}/chrome/JS/zenleap_agent.uc.js")
                status=" ${GREEN}(agent v$v installed)${NC}"
            fi
            # Check if fx-autoconfig is present
            if [ -f "${PROFILES[$i]}/chrome/utils/boot.sys.mjs" ]; then
                status="$status ${DIM}[fx-autoconfig]${NC}"
            fi
            echo -e "  $((i+1)). $pname$status"
        done
        echo ""
        echo -n "Select profile (1-${#PROFILES[@]}): "
        read -r selection <&3
        if ! [[ "$selection" =~ ^[0-9]+$ ]] || [ "$selection" -lt 1 ] || [ "$selection" -gt ${#PROFILES[@]} ]; then
            echo -e "${RED}Invalid selection${NC}"
            exit 1
        fi
        SELECTED_PROFILES=("${PROFILES[$((selection-1))]}")
        echo -e "${GREEN}+${NC} Selected: $(basename "${SELECTED_PROFILES[0]}")"
    fi
}

# Set per-profile path variables for a given profile directory
set_profile_paths() {
    PROFILE_DIR="$1"
    CHROME_DIR="$PROFILE_DIR/chrome"
    JS_DIR="$CHROME_DIR/JS"
    ACTORS_DIR="$JS_DIR/actors"
}

check_fxautoconfig() {
    if [ -f "$CHROME_DIR/utils/boot.sys.mjs" ] || [ -f "$CHROME_DIR/utils/chrome.manifest" ]; then
        echo -e "${GREEN}+${NC} fx-autoconfig detected"
        return 0
    fi
    echo -e "${RED}Error: fx-autoconfig not found in this profile.${NC}"
    echo ""
    echo "fx-autoconfig is required to load .uc.js extensions."
    echo "Install it from: https://github.com/MrOtherGuy/fx-autoconfig"
    echo "Or install ZenLeap first (which includes fx-autoconfig)."
    return 1
}

check_zen_running() {
    if pgrep -x "zen" > /dev/null 2>&1 || pgrep -x "Zen Browser" > /dev/null 2>&1 || pgrep -x "Twilight" > /dev/null 2>&1; then
        echo -e "${YELLOW}! Zen Browser is running${NC}"
        if [ "$AUTO_YES" = true ]; then
            echo "  Closing Zen Browser..."
        else
            echo -n "  Close Zen Browser to continue? (y/n): "
            read -r response <&3
            if [ "$response" != "y" ] && [ "$response" != "Y" ]; then
                echo "Please close Zen Browser and try again."
                exit 1
            fi
        fi
        if [ "$OS" = "macos" ]; then
            osascript -e 'quit app "Zen"' 2>/dev/null || osascript -e 'quit app "Zen Browser"' 2>/dev/null || osascript -e 'quit app "Twilight"' 2>/dev/null || true
        else
            pkill -x "zen" 2>/dev/null || true
        fi
        sleep 2
    fi
}

clear_cache() {
    local cache_dir=""
    if [ "$OS" = "macos" ]; then
        cache_dir="$HOME/Library/Caches/zen"
    else
        cache_dir="$HOME/.cache/zen"
    fi
    if [ -d "$cache_dir" ]; then
        rm -rf "$cache_dir/startupCache" 2>/dev/null || true
        echo -e "${GREEN}+${NC} Startup cache cleared"
    fi
}

install_to_profile() {
    # Install agent files to a single profile (PROFILE_DIR must be set)
    set_profile_paths "$1"
    local pname
    pname=$(basename "$1")

    echo ""
    echo -e "${BLUE}--- $pname ---${NC}"

    if ! check_fxautoconfig; then
        echo -e "  ${YELLOW}!${NC} Skipping this profile (fx-autoconfig required)"
        return 1
    fi

    # Create directories
    mkdir -p "$JS_DIR"
    mkdir -p "$ACTORS_DIR"

    # Guard: detect Sine-managed config.mjs — never overwrite it
    if [ -f "$CHROME_DIR/config.mjs" ]; then
        echo -e "  ${DIM}config.mjs detected (Sine or custom loader) — preserving${NC}"
    fi

    # Check for existing installation
    if [ -f "$JS_DIR/zenleap_agent.uc.js" ]; then
        local existing_v
        existing_v=$(get_version "$JS_DIR/zenleap_agent.uc.js")
        local new_v
        new_v=$(get_version "$SCRIPT_DIR/browser/zenleap_agent.uc.js")
        echo -e "  ${YELLOW}!${NC} Existing installation found (v$existing_v)"
        if [ "$AUTO_YES" != true ]; then
            echo -n "  Overwrite with v$new_v? (y/n): "
            read -r response <&3
            if [ "$response" != "y" ] && [ "$response" != "Y" ]; then
                echo "  Skipped."
                return 0
            fi
        else
            echo "  Overwriting with v$new_v"
        fi
    fi

    # Copy agent script
    cp "$SCRIPT_DIR/browser/zenleap_agent.uc.js" "$JS_DIR/zenleap_agent.uc.js"
    echo -e "  ${GREEN}+${NC} Installed zenleap_agent.uc.js"

    # Copy actor files
    cp "$SCRIPT_DIR/browser/actors/ZenLeapAgentChild.sys.mjs" "$ACTORS_DIR/ZenLeapAgentChild.sys.mjs"
    cp "$SCRIPT_DIR/browser/actors/ZenLeapAgentParent.sys.mjs" "$ACTORS_DIR/ZenLeapAgentParent.sys.mjs"
    echo -e "  ${GREEN}+${NC} Installed JSWindowActor modules"

    # Verify copies
    local ok=true
    diff -q "$SCRIPT_DIR/browser/zenleap_agent.uc.js" "$JS_DIR/zenleap_agent.uc.js" > /dev/null 2>&1 || ok=false
    diff -q "$SCRIPT_DIR/browser/actors/ZenLeapAgentChild.sys.mjs" "$ACTORS_DIR/ZenLeapAgentChild.sys.mjs" > /dev/null 2>&1 || ok=false
    diff -q "$SCRIPT_DIR/browser/actors/ZenLeapAgentParent.sys.mjs" "$ACTORS_DIR/ZenLeapAgentParent.sys.mjs" > /dev/null 2>&1 || ok=false

    if [ "$ok" = false ]; then
        echo -e "  ${RED}Error: File verification failed! Copies may be corrupted.${NC}"
        return 1
    fi
    echo -e "  ${GREEN}+${NC} File integrity verified"
}

do_install() {
    detect_os
    find_profiles
    select_profiles

    # Track if Zen was running before we close it
    ZEN_WAS_RUNNING=false
    if pgrep -x "zen" > /dev/null 2>&1 || pgrep -x "Zen Browser" > /dev/null 2>&1 || pgrep -x "Twilight" > /dev/null 2>&1; then
        ZEN_WAS_RUNNING=true
    fi

    check_zen_running

    echo ""
    echo -e "${BLUE}Installing Zen AI Agent...${NC}"

    # Install to each selected profile
    local install_failed=false
    for profile in "${SELECTED_PROFILES[@]}"; do
        install_to_profile "$profile" || install_failed=true
    done

    if [ "$install_failed" = true ]; then
        echo -e "${RED}Some profiles failed to install. Check errors above.${NC}"
        exit 1
    fi

    clear_cache

    local installed_v
    installed_v=$(get_version "$SCRIPT_DIR/browser/zenleap_agent.uc.js")

    echo ""
    echo -e "${GREEN}======================================================${NC}"
    echo -e "${GREEN}  Zen AI Agent v$installed_v installed successfully!${NC}"
    echo -e "${GREEN}======================================================${NC}"
    echo ""
    echo -e "${BLUE}Next steps:${NC}"
    echo "  1. Restart Zen Browser"
    echo "  2. Set up the MCP server for Claude Code:"
    echo "     cd $(basename "$SCRIPT_DIR")/mcp && uv sync"
    echo "  3. Add to your Claude Code project's .mcp.json:"
    echo "     {\"mcpServers\": {\"zenleap-browser\": {"
    echo "       \"command\": \"uv\","
    echo "       \"args\": [\"run\", \"--project\", \"$SCRIPT_DIR/mcp\", \"python\", \"$SCRIPT_DIR/mcp/zenleap_mcp_server.py\"]"
    echo "     }}}"
    echo ""
    echo -e "${DIM}The agent runs a WebSocket server on localhost:9876${NC}"
    echo ""

    # Relaunch if it was running, or offer to open
    if [ "$ZEN_WAS_RUNNING" = true ]; then
        echo -e "${BLUE}Restarting Zen Browser...${NC}"
        if [ "$OS" = "macos" ]; then
            open -a "Zen" 2>/dev/null || open -a "Zen Browser" 2>/dev/null || open -a "Twilight" 2>/dev/null || true
        else
            zen &
        fi
    elif [ "$AUTO_YES" != true ]; then
        echo -n "Open Zen Browser now? (y/n): "
        read -r response <&3
        if [ "$response" = "y" ] || [ "$response" = "Y" ]; then
            if [ "$OS" = "macos" ]; then
                open -a "Zen" 2>/dev/null || open -a "Zen Browser" 2>/dev/null || true
            else
                zen &
            fi
        fi
    fi
}

do_uninstall() {
    detect_os
    find_profiles
    select_profiles
    check_zen_running

    echo ""
    echo -e "${BLUE}Uninstalling Zen AI Agent...${NC}"

    for profile in "${SELECTED_PROFILES[@]}"; do
        set_profile_paths "$profile"
        local pname
        pname=$(basename "$profile")
        echo ""
        echo -e "${BLUE}--- $pname ---${NC}"

        local found=false

        if [ -f "$JS_DIR/zenleap_agent.uc.js" ]; then
            rm -f "$JS_DIR/zenleap_agent.uc.js"
            echo -e "  ${GREEN}+${NC} Removed zenleap_agent.uc.js"
            found=true
        fi

        if [ -f "$ACTORS_DIR/ZenLeapAgentChild.sys.mjs" ]; then
            rm -f "$ACTORS_DIR/ZenLeapAgentChild.sys.mjs"
            echo -e "  ${GREEN}+${NC} Removed ZenLeapAgentChild.sys.mjs"
            found=true
        fi

        if [ -f "$ACTORS_DIR/ZenLeapAgentParent.sys.mjs" ]; then
            rm -f "$ACTORS_DIR/ZenLeapAgentParent.sys.mjs"
            echo -e "  ${GREEN}+${NC} Removed ZenLeapAgentParent.sys.mjs"
            found=true
        fi

        # Clean up empty actors directory
        if [ -d "$ACTORS_DIR" ] && [ -z "$(ls -A "$ACTORS_DIR")" ]; then
            rmdir "$ACTORS_DIR"
        fi

        if [ "$found" = true ]; then
            echo -e "  ${GREEN}+${NC} Agent removed"
        else
            echo -e "  ${DIM}Zen AI Agent was not installed in this profile.${NC}"
        fi
    done

    clear_cache
    echo ""
    echo -e "${GREEN}Uninstallation complete.${NC}"
    echo -e "${YELLOW}Restart Zen Browser for changes to take effect.${NC}"
}

do_list() {
    detect_os
    find_profiles

    echo -e "${BLUE}Zen AI Agent Installation Status${NC}"
    echo ""
    for i in "${!PROFILES[@]}"; do
        local pname
        pname=$(basename "${PROFILES[$i]}")
        local agent_file="${PROFILES[$i]}/chrome/JS/zenleap_agent.uc.js"
        if [ -f "$agent_file" ]; then
            local v
            v=$(get_version "$agent_file")
            echo -e "  ${GREEN}+${NC} $pname — ${CYAN}v$v${NC} installed"
        else
            echo -e "  ${DIM}-${NC} $pname — not installed"
        fi
    done
    echo ""
}

# Parse arguments
while [ $# -gt 0 ]; do
    case "$1" in
        --profile)
            shift
            if [ -z "$1" ] || [[ "$1" == --* ]]; then
                echo -e "${RED}Error: --profile requires an index${NC}"
                exit 1
            fi
            PROFILE_INDEX="$1"
            ;;
        --yes|-y)
            AUTO_YES=true
            ;;
        --uninstall|--remove)
            UNINSTALL_MODE=true
            ;;
        --list)
            LIST_MODE=true
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Installs the Zen AI Agent browser automation server."
            echo ""
            echo "Options:"
            echo "  --profile <index>   Select profile by index (1-based)"
            echo "  --yes, -y           Auto-confirm all prompts"
            echo "  --uninstall         Remove the agent"
            echo "  --list              Show installed status"
            echo "  --help, -h          Show this help"
            echo ""
            echo "Examples:"
            echo "  $0                          Interactive install"
            echo "  $0 --profile 1 --yes        Non-interactive install to profile 1"
            echo "  $0 --uninstall --profile 1  Uninstall from profile 1"
            echo "  $0 --list                   Show status"
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            echo "Use --help for usage."
            exit 1
            ;;
    esac
    shift
done

# Execute
if [ "$LIST_MODE" = true ]; then
    do_list
elif [ "$UNINSTALL_MODE" = true ]; then
    show_banner
    do_uninstall
else
    show_banner
    do_install
fi
