# MTG Collection & Commander Deck Manager (MTGINV)

A local, private, and powerful **Magic: The Gathering** collection tracker and Commander/EDH deck builder built with Python and [Streamlit](https://streamlit.io/).

---

## Key Features

- **Collection Management**:
  - Track owned cards with quantities, foil status, sets, language, and condition.
  - Automatic metadata enrichment from the official [Scryfall API](https://scryfall.com/) (oracle text, colors, keywords, high-res art, prices).
- **Commander Deck Builder**:
  - Visual deck cards showing commander art, color identities, card counts, and deck statistics.
  - Dedicated Partner / Background commander support.
  - Clean card picker with a **"Hide cards already in deck"** toggle.
  - Uniform visual card gallery with aligned keyword badges and power/toughness tags.
- **High-Speed Mass Importer**:
  - Import raw decklists or collection dumps directly.
  - Automatic batch fetching using Scryfall's Collection API (up to 75 cards per batch) with polite rate-limiting.
  - Automatic comment and set code stripping (`// Commander`, `(CMR) 351`, `*F*`).
- **Visual Analytics**:
  - Mana curve distribution charts, color pie breakdowns, and card type statistics.
- **Integrated Diagnostics**:
  - Built-in logging (`mtg_app.log`) with an in-app viewer in the sidebar to inspect activity, batch requests, and errors.
- **AI Deck Optimizer (Optional - Powered by Ollama)**:
  - 100% local, private AI analysis that suggests synergies, card cuts, and deck upgrades strictly using cards present in your owned collection.

---

## Quick Start (Windows)

The repository includes automated batch scripts that handle everything for you:

### 1. Install Dependencies
Double-click:
```bat
install.bat
```
*What it does:*
- Detects if Python is installed on your machine.
- **If Python is missing**, it automatically installs Python 3.12 (via Windows Package Manager `winget` or silent download from python.org).
- Creates an isolated virtual environment (`.venv`).
- Automatically installs all required libraries (`streamlit`, `pandas`, `requests`, `altair`).

### 2. Launch the Application
Double-click:
```bat
run.bat
```
*(or `launch.bat`)*

This starts the local Streamlit server and opens the application in your default web browser (typically at `http://localhost:8501`).

---

## Manual Installation (Cross-Platform: Windows / macOS / Linux)

If you prefer to set up manually or are on macOS / Linux:

1. **Clone the repository:**
   ```bash
   git clone https://github.com/4nietodev/MTGINV.git
   cd MTGINV
   ```

2. **Create and activate a virtual environment:**
   ```bash
   # Windows
   python -m venv .venv
   .venv\Scripts\activate

   # macOS / Linux
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Run the application:**
   ```bash
   streamlit run app.py
   ```

---

## AI Deck Optimizer with Ollama (OPTIONAL)

> [!NOTE]
> **Ollama is 100% OPTIONAL.** The entire MTG Inventory & Deck Builder application works completely without Ollama. You only need Ollama if you wish to use the **"🤖 AI Optimizer (Ollama)"** tab inside the Deck Builder to generate local AI recommendations based on your collection.

### What does it do?
The AI optimizer queries a local Large Language Model (LLM) running on your computer to:
- Review your Commander's synergy and strategy.
- Compare your deck against all unused cards in your physical inventory.
- Suggest recommended additions from your collection and suggested cuts.
- **Zero cloud calls**: Your cards and data never leave your PC.

---

### How to Set Up Ollama (Step-by-Step)

#### Step 1: Install Ollama
Download and install Ollama for your operating system:
- **Windows / macOS / Linux**: Download the installer from the official website at [ollama.com/download](https://ollama.com/download).
- *Alternatively on Windows (via PowerShell / Command Prompt)*:
  ```powershell
  winget install Ollama.Ollama
  ```

#### Step 2: Download a Model
Open a terminal (Command Prompt, PowerShell, or Bash) and pull your preferred model. Recommended models for MTG card analysis:

- **Llama 3 (Recommended - balanced speed & reasoning)**:
  ```bash
  ollama pull llama3
  ```
- **Mistral (Great alternative)**:
  ```bash
  ollama pull mistral
  ```
- **Llama 3.1 (High reasoning capacity)**:
  ```bash
  ollama pull llama3.1
  ```
- **Phi-3 (Lightweight for lower-spec PCs)**:
  ```bash
  ollama pull phi3
  ```

#### Step 3: Ensure Ollama is Running
Ollama typically starts automatically as a background service on Windows (visible in your system tray). If it is not running, start it in your terminal:
```bash
ollama serve
```

#### Step 4: Use it in the App!
1. Open the MTGINV app and navigate to **🎴 Decks**.
2. Select or open any deck.
3. Switch to the **"🤖 AI Optimizer (Ollama)"** tab.
4. The app will detect your local Ollama server (`http://localhost:11434`) and list all models you have pulled.
5. Select your model and click **Analyze & Optimize Deck with Ollama**.

---

## Project Structure

```text
MTGINV/
├── app.py                # Main Streamlit web application & SQLite logic
├── requirements.txt      # Python dependencies
├── install.bat           # Automated installer (with Python auto-install)
├── run.bat / launch.bat  # 1-click application launcher
├── mtg_inventory.db      # Local SQLite database (auto-generated, git-ignored)
├── mtg_app.log           # Application diagnostic log (auto-generated)
├── .gitignore            # Git rules excluding database, logs, and venvs
└── README.md             # Project documentation
```

---

## Privacy & Local Data Storage

- All inventory data, decklists, and custom notes are stored locally in `mtg_inventory.db` using SQLite.
- External network requests are restricted to public card metadata queries via the Scryfall API.
- If Ollama is used, all prompt processing and inference run completely on your local machine.

---

## License

This project is open-source under the MIT License. Magic: The Gathering is a trademark of Wizards of the Coast LLC. This project is not affiliated with Wizards of the Coast.
