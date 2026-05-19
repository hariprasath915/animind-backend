# pdf_handler.py — EduAnimator PDF Processing (Reframed for Subtopics Workflow)
# ===============================================
# New workflow:
# 1. extract_pdf_text(pdf_bytes) → dict (unchanged)
# 2. find_subtopics_in_pdf(full_text, main_topic) → dict 
#    - Finds main section for main_topic
#    - Extracts ONLY subtopics under that section (no full content)
#    - Returns structured subtopics by input sub-query
# 3. build_subtopics_json(main_topic, pdf_data) → str (JSON for sub_topics.py)
import re
import io
import json
from typing import Union, Dict, List
from collections import defaultdict

# ════════════════════════════════════════════════════════════════════════
# FUNCTION 1 — EXTRACT TEXT FROM PDF (UNCHANGED)
# ════════════════════════════════════════════════════════════════════════
def extract_pdf_text(pdf_file_bytes: bytes) -> dict:
    """
    Extract all text from a PDF file.
    Uses pdfplumber (primary) with PyPDF2 as fallback.
    """
    # ── Primary: pdfplumber ──────────────────────────────────────────────
    try:
        import pdfplumber
        pages_text = []
        with pdfplumber.open(io.BytesIO(pdf_file_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                pages_text.append(text)

        full_text = "\n\n".join(p for p in pages_text if p.strip())
        word_count = len(full_text.split())

        print(f"[PDF]     pdfplumber ✅  {len(pages_text)} pages  {word_count:,} words")
        return {
            "full_text": full_text,
            "pages": pages_text,
            "page_count": len(pages_text),
            "word_count": word_count,
            "success": True,
            "error": None,
        }

    except Exception as primary_err:
        print(f"[PDF]     pdfplumber failed: {primary_err} — trying PyPDF2 fallback")

    # ── Fallback: PyPDF2 ─────────────────────────────────────────────────
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_file_bytes))
        pages_text = []
        for page in reader.pages:
            text = page.extract_text() or ""
            pages_text.append(text)

        full_text = "\n\n".join(p for p in pages_text if p.strip())
        word_count = len(full_text.split())

        print(f"[PDF]     PyPDF2 fallback ✅  {len(pages_text)} pages  {word_count:,} words")
        return {
            "full_text": full_text,
            "pages": pages_text,
            "page_count": len(pages_text),
            "word_count": word_count,
            "success": True,
            "error": None,
        }

    except Exception as fallback_err:
        print(f"[PDF]     Both extractors failed: {fallback_err}")
        return {
            "full_text": "",
            "pages": [],
            "page_count": 0,
            "word_count": 0,
            "success": False,
            "error": str(fallback_err),
        }

# ════════════════════════════════════════════════════════════════════════
# FUNCTION 2 — FIND SUBTOPICS FOR MAIN TOPIC (NEW)
# ════════════════════════════════════════════════════════════════════════
def find_subtopics_in_pdf(full_text: str, main_topic: str) -> dict:
    """
    New workflow: Find subtopics under main_topic (e.g., "optical fibre").
    - Locate main section for main_topic
    - Extract subtopics under it (e.g., "fundamental of optical fibre", "features of optical fibre", "losses in optical fibre")
    - Group subtopics by input sub-queries (e.g., "losses in fibre" → its sub-subtopics)
    - Returns ONLY subtopic lists, no full content

    Returns:
        {
            "main_topic": str,
            "main_headings": list[str],
            "subtopics_by_query": dict[str, list[str]],  # e.g., {"losses in fibre": ["absorption losses", "scattering losses"]}
            "all_subtopics": list[str],
            "coverage_score": float,
            "page_range": str
        }
    """
    if not full_text.strip():
        return _empty_subtopics_result(main_topic)

    topic_lower = main_topic.lower()
    main_keywords = _extract_keywords(topic_lower)
    print(f"[PDF]     Searching subtopics for main topic: '{main_topic}'")

    # ── Step 1: Find main section headings ───────────────────────────────
    heading_pattern = re.compile(
        r'^(?:chapter|section|module|unit)?\s*[\d.\-]+?\s*'
        r'(?:' + '|'.join(re.escape(kw) for kw in main_keywords) + r')[\s:\-]*',
        re.IGNORECASE | re.MULTILINE
    )
    main_headings = [h.strip() for h in heading_pattern.findall(full_text)[:5]]
    
    if not main_headings:
        return _empty_subtopics_result(main_topic)

    # ── Step 2: Extract section text after first main heading ────────────
    first_heading = main_headings[0]
    match = re.search(re.escape(first_heading), full_text, re.IGNORECASE)
    if not match:
        return _empty_subtopics_result(main_topic)
    
    section_text = full_text[match.end():match.end() + 50000].strip()  # ~50k chars after heading
    words = section_text.split()
    total_words = len(words)

    # ── Step 3: Extract subtopics using multiple patterns ────────────────
    subtopic_patterns = [
        r'^\s*[\d\.\-]+\s+([A-Z][a-zA-Z\s]{5,50})(?=\s|$)',  # 1.1 Feature of Optical fibers
        r'^\s*-?\s*([A-Z][a-zA-Z\s\-]{5,50})(?=\s*[\d\.\-])',  # - Features of Optical fibers
        r'(?:introduction|fundamentals?|features?|types?|losses?|dispersion|applications?)\s+of\s+',
        r'([A-Z][a-zA-Z\s\-]{5,50})(?:\n|\.|$)(?=\s*[\d\.\-])'  # Standalone subheadings
    ]
    
    all_subtopics = set()
    for pat in subtopic_patterns:
        matches = re.findall(pat, section_text, re.IGNORECASE | re.MULTILINE)
        for m in matches:
            clean = re.sub(r'[:\-]+$', '', m.strip()).title()
            if len(clean) > 5 and clean.lower() not in ['module', 'chapter', 'contents']:
                all_subtopics.add(clean)

    all_subtopics = sorted(list(all_subtopics))[:20]  # Cap at 20

    # ── Step 4: Group subtopics by potential input sub-queries ───────────
    # Common sub-queries derived from main_topic context (customize as needed)
    potential_sub_queries = _derive_sub_queries(main_keywords, all_subtopics)
    subtopics_by_query = {}
    
    for sub_query, sub_keywords in potential_sub_queries.items():
        query_hits = []
        for subt in all_subtopics:
            score = sum(1 for kw in sub_keywords if kw in subt.lower())
            if score >= 1:
                query_hits.append(subt)
        subtopics_by_query[sub_query] = sorted(query_hits)[:10]

    # ── Step 5: Coverage & page range ────────────────────────────────────
    coverage_score = _compute_subtopics_coverage(len(main_headings), len(all_subtopics), len(subtopics_by_query))
    page_range = _estimate_page_range(full_text, main_keywords)

    print(f"[PDF]     Found {len(all_subtopics)} subtopics under '{main_topic}' | score={coverage_score:.2f}")

    return {
        "main_topic": main_topic,
        "main_headings": main_headings,
        "all_subtopics": all_subtopics,
        "subtopics_by_query": subtopics_by_query,
        "coverage_score": coverage_score,
        "page_range": page_range,
    }

# ════════════════════════════════════════════════════════════════════════
# FUNCTION 3 — BUILD JSON FOR SUB_TOPICS.PY (NEW)
# ════════════════════════════════════════════════════════════════════════
def build_subtopics_json(main_topic: str, pdf_data: dict) -> str:
    """
    Build JSON string for sub_topics.py consumption.
    Format:
    {
      "main_topic": "optical fibre",
      "all_subtopics": ["Fundamentals of Fibre Optics", "Features of Optical Fibres", "Losses Associated with Optical Fibers"],
      "subtopics_by_query": {
        "losses in fibre": ["Absorption Losses", "Scattering Losses", "Bending Losses"],
        "fundamental of optical fibre": ["Principle and Propagation of Light"]
      }
    }
    """
    data = {
        "main_topic": pdf_data.get("main_topic", main_topic),
        "all_subtopics": pdf_data.get("all_subtopics", []),
        "subtopics_by_query": pdf_data.get("subtopics_by_query", {}),
    }
    
    json_str = json.dumps(data, indent=2, ensure_ascii=False)
    print(f"[PDF]     JSON ready: {len(data['all_subtopics'])} subtopics for '{main_topic}'")
    return json_str

# ════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS (UPDATED)
# ════════════════════════════════════════════════════════════════════════
def _extract_keywords(topic_lower: str) -> list:
    """Break topic into meaningful keywords."""
    STOP = {
        'a','an','the','of','in','on','at','by','to','for','and','or','with','from','is','are','was','this','that',
        'its','it','as','be','into','not','use','uses','using','the','module','chapter'
    }
    words = re.split(r'[\s,/\-]+', topic_lower)
    keywords = [w for w in words if w and w not in STOP and len(w) > 2]
    if len(keywords) > 1:
        keywords.insert(0, topic_lower.replace(' ', '-'))
    return keywords[:10]

def _derive_sub_queries(main_keywords: list, all_subtopics: list) -> Dict[str, list]:
    """Derive common sub-queries from context (e.g., for optical fibre)."""
    sub_queries = {}
    common_subs = ['fundamental', 'features', 'principle', 'types', 'losses', 'dispersion', 'applications']
    
    for sub in common_subs:
        if any(sub in st.lower() for st in all_subtopics):
            sub_keywords = _extract_keywords(sub)
            sub_queries[sub.replace('fundamental', 'fundamentals of optical fibre').replace('features', 'features of optical fibre')] = sub_keywords
    
    # Custom for optical fibre example
    if 'fibre' in ' '.join(main_keywords).lower() or 'fiber' in ' '.join(main_keywords).lower():
        sub_queries.update({
            "fundamental of optical fibre": ['fundamental', 'fundamentals', 'principle', 'propagation'],
            "features of optical fibre": ['features', 'feature', 'advantages'],
            "losses in fibre": ['losses', 'absorption', 'scattering', 'bending'],
            "types of optical fibre": ['types', 'single', 'multi', 'step', 'graded']
        })
    
    return sub_queries

def _compute_subtopics_coverage(num_headings: int, num_subtopics: int, num_queries: int) -> float:
    """Simple coverage score for subtopics."""
    score = 0.0
    if num_headings >= 1: score += 0.3
    if num_subtopics >= 5: score += 0.4
    if num_subtopics >= 10: score += 0.2
    if num_queries >= 2: score += 0.1
    return min(score, 1.0)

def _estimate_page_range(full_text: str, keywords: list) -> str:
    """Estimate page range (unchanged)."""
    # ... (same as original)
    page_markers = re.findall(r'(?:page\s*)?(\d{1,4})(?=\s*\n)', full_text, re.IGNORECASE)
    if not page_markers:
        return "pp. (unknown)"
    return "pp. (estimated from content)"

def _empty_subtopics_result(main_topic: str) -> dict:
    return {
        "main_topic": main_topic,
        "main_headings": [],
        "all_subtopics": [],
        "subtopics_by_query": {},
        "coverage_score": 0.0,
        "page_range": "pp. (not found)",
    }