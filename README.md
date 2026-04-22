🦊 Camoufox WRB (Web Remote Browser)

Camoufox WRB is a high-performance, lightweight Web-based profile manager for Camoufox. It provides an intuitive dashboard to manage multiple browser environments with professional-grade fingerprint protection and secure network routing.

    Note: This is the Web Manager (WRB) version. A Native Desktop version is currently in the planning stage.

✨ Key Features

    🛡️ Advanced Fingerprint Spoofing:

        Font Protection: Automatically loads a system-specific font whitelist from fonts.json to prevent leaking host fonts (crucial for Linux power users).

        Hardware Emulation: Spoof WebGL vendors, renderers, and CPU hardware concurrency (navigator.hardwareConcurrency).

    🌐 Secure Network Routing:

        Anti-DNS Leak: Automatically converts socks5 to socks5h to force remote DNS resolution at the proxy node, ensuring a 100% clean test on BrowserScan.

        Integrated Proxy Tester: Uses curl to verify connectivity, latency, and geolocational data (ISP, City, Country) for HTTP/SOCKS proxies.

    🧩 Extension Store Integration:

        AMO Search: Browse and search the official Firefox Add-ons (AMO) store directly from the dashboard.

        One-Click Install: Download .xpi files and distribute them to specific profiles with a single click.

    💾 Smart Session Memory:

        Session Restore: Automatically records and restores the last open tabs upon the next launch.

    ⚙️ System Stability:

        Async Log System: Redirects browser stdout/stderr to browser.log files to prevent pipe buffer deadlocks and sudden window crashes.

        Zombie Killer: Built-in utility to scan and terminate untracked or orphaned browser processes.

🚀 Getting Started
Prerequisites

    Python 3.10+

    curl (required for proxy connectivity tests)

Installation

    Clone the repository:
    Bash

    git clone https://github.com/smithyyang/camoufox-manager-web.git
    cd camoufox-manager-web

    Set up Virtual Environment:
    Bash

    python3 -m venv venv
    source venv/bin/activate  # Linux/macOS
    # .\venv\Scripts\activate # Windows

    Install Dependencies:
    Bash

    pip install -r requirements.txt

    Fetch Browser Binaries (Required for first-time use):
    Bash

    python3 -m camoufox fetch

Run the Application
Bash

python3 app.py

Access the dashboard at http://localhost:7070.
📂 Project Structure

    app.py: Flask backend managing profile lifecycles and AMO API integration.

    profiles_data/: Local storage for environment metadata and browser userdata (automatically ignored by Git).

    extensions/: Local repository for downloaded .xpi extension files.

    templates/index.html: Modern, dark-themed dashboard UI.

📄 License

This project is licensed under the MIT License.
