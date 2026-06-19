# sub_topics.py — Process JSON from pdf_handler.py
# ================================================
# Receives JSON from pdf_handler.build_subtopics_json()
# Formats and presents subtopics to frontend/AI
import json
import sys
from typing import Dict, Any

def process_subtopics_json(json_str: str) -> Dict[str, Any]:
    """
    Parse JSON from pdf_handler and format for presentation.
    
    Example input JSON:
    {
      "main_topic": "optical fibre",
      "all_subtopics": ["Fundamentals of Fibre Optics", "Features of Optical Fibres", "Losses Associated with Optical Fibers"],
      "subtopics_by_query": {
        "losses in fibre": ["Absorption Losses", "Scattering Losses", "Bending Losses"],
        "fundamental of optical fibre": ["Principle and Propagation of Light"]
      }
    }
    
    Returns formatted dict for frontend (e.g., present as dropdowns/lists).
    """
    try:
        data = json.loads(json_str)
        
        formatted = {
            "status": "success" if data.get("coverage_score", 0) > 0.2 else "partial",
            "main_topic": data.get("main_topic", "Unknown"),
            "coverage_score": round(data.get("coverage_score", 0), 2),
            "page_range": data.get("page_range", "unknown"),
            "total_subtopics": len(data.get("all_subtopics", [])),
            "all_subtopics": data.get("all_subtopics", []),
            "grouped_subtopics": data.get("subtopics_by_query", {}),
            "message": f"Found {len(data.get('all_subtopics', []))} subtopics under '{data['main_topic']}'"
        }
        
        print(f"[SUBTOPICS] Processed: {formatted['total_subtopics']} subtopics | score={formatted['coverage_score']}")
        return formatted
        
    except json.JSONDecodeError as e:
        print(f"[SUBTOPICS] JSON error: {e}")
        return {"status": "error", "message": "Invalid JSON from pdf_handler"}

# ══ USAGE EXAMPLE ════════════════════════════════════════════════════════
# In your /generate-from-book endpoint:
# 1. pdf_data = find_subtopics_in_pdf(full_text, user_main_topic)  # e.g., "optical fibre"
# 2. subtopics_json = build_subtopics_json(user_main_topic, pdf_data)
# 3. formatted = process_subtopics_json(subtopics_json)  # Send to frontend
# 4. Frontend shows: "Optical Fibre Subtopics: fundamentals..., features..., losses..."
#
# For user input "optical fibre - losses in fibre":
# - main_topic = "optical fibre"
# - Shows grouped: losses in fibre: [absorption, scattering...]