# OmniExtract: Enterprise RFP Intelligence Extractor

🚀 **Live Demo:** [https://omniextract.streamlit.app/](https://omniextract.streamlit.app/)

OmniExtract is a high-performance, AI-powered data pipeline designed to parse, analyze, and intelligently extract highly structured JSON data from complex Government Procurement and Request for Proposal (RFP) documents. 

Rather than relying on Retrieval-Augmented Generation (RAG) which often misses structured data, OmniExtract uses a robust **Parallel Map-Reduce LLM Architecture** combined with **Concept Remembering** to achieve maximum data extraction accuracy across multiple associated documents (e.g., Base RFPs and their subsequent Addendums).

## Key Features

- **Multi-Format Parsing:** Seamlessly processes PDF, HTML, and DOCX files. Powered by `PyMuPDF` (`fitz`) for lightning-fast (< 1 second) multi-page PDF processing.
- **Parallel AI Engine:** Breaks massive documents into boundary-aware chunks and processes them simultaneously via threaded workers, avoiding the token-loss associated with standard truncation.
- **Concept Remembering:** Dynamically merges and intelligently overrides data when reading Addendums. If Addendum 1 changes the "Due Date", the system updates that specific field while preserving the original Base RFP context.
- **Production Resilience:** Built-in safeguards including automated chunk slicing for `413 Request Too Large` errors, exponential backoffs for API rate limits, and an SHA-256 caching layer to prevent redundant API calls.
- **Enterprise Dashboard:** A beautiful, responsive Streamlit dashboard featuring a live macOS-style terminal that renders the JSON extraction in real-time.

---

## Installation & Setup

### Prerequisites
- Python 3.10+
- A valid Groq API Key

### 1. Clone & Install Dependencies
Clone the repository and install the required Python packages:

```bash
git clone <repository-url>
cd rfp-intelligence-extractor
pip install -r requirements.txt
```

### 2. Configure Environment Variables
Create a `.env` file in the root directory and add your Groq API Key:

```env
GROQ_API_KEY=gsk_your_api_key_here
```
*(Alternatively, you can export it directly in your terminal: `export GROQ_API_KEY=...` or `$env:GROQ_API_KEY="..."` on Windows).*

---

## Running the Application

To launch the Enterprise Dashboard, run the following command from the root directory:

```bash
streamlit run app.py
```

1. Open your web browser to `http://localhost:8501`.
2. Drag and drop your RFP documents into the upload box. **Important:** If you have Addendums, upload them *at the same time* as the Base RFP.
3. Click **Process Documents**.
4. Monitor the live terminal as the parallel threads extract and merge the intelligence into structured JSON.
5. Download the final JSON or CSV report using the export buttons.

---

## Architecture Overview

For a detailed breakdown of the technical decisions (including why RAG was intentionally avoided for this assignment in favor of deterministic parallel processing), please refer to the `IMPLEMENTATION_PLAN.md` file included in this repository.

### Core Modules:
- **`app.py`:** The Streamlit controller. Manages UI state, live streaming loops, and orchestrates the extraction logic.
- **`extractor.py`:** The AI brain. Contains the ThreadPoolExecutor logic, Groq LLM API integrations, dynamic error slicing, and the determinative `smart_merge` logic.
- **`parser.py`:** The ingestion engine. Uses `PyMuPDF` and `BeautifulSoup` to strip, read, and intelligently chunk documents without breaking critical table structures.
- **`logger.py`:** A production-grade custom logger providing thread-safe, colored console outputs and rotating JSON log files for debugging API limits.
