"""Reference libraries the copilot searches: reviewed safety observations and
HIRA rows from a plant's own registers, plus good-practice control ideas.

They are read-only JSON files under ./data, built offline by
scripts/copilot_knowledge/build_library.py. No personal names, employee ids or
raw free text from the source registers are stored.
"""

from app.services.copilot.knowledge import hira_library, observations

__all__ = ["hira_library", "observations"]
