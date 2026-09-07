from dataclasses import dataclass
import re
from typing import Any, Dict, List, Optional, Tuple

from agent.skills.schema import SkillManifest


@dataclass
class RetrievedSkillMatch:
    skill: SkillManifest
    confidence: float
    extracted_params: Dict[str, Any]
    match_reason: str


def _tokenize(text: str) -> set:
    words = re.findall(r"[a-z0-9]+", text.lower())
    # remove common stop words
    stop = {"a", "an", "the", "in", "on", "at", "to", "for", "with", "and", "or", "of", "my", "me", "please", "can", "you", "i", "want"}
    return {w for w in words if w not in stop and len(w) > 1}


def calculate_similarity(query: str, skill: SkillManifest) -> float:
    """Calculate semantic/keyword similarity between query and skill."""
    q_tokens = _tokenize(query)
    if not q_tokens:
        return 0.0

    # Skill tokens from name, description, parameters, commands
    name_tokens = _tokenize(skill.name.replace("_", " ").replace("-", " "))
    desc_tokens = _tokenize(skill.description)
    target_tokens = name_tokens | desc_tokens
    for p in skill.parameters.keys():
        target_tokens |= _tokenize(p.replace("_", " "))

    overlap = q_tokens & target_tokens
    if not overlap:
        return 0.0

    # Jaccard + name weight
    name_overlap = q_tokens & name_tokens
    score = (len(overlap) / len(q_tokens | target_tokens)) * 0.6
    if name_overlap:
        score += (len(name_overlap) / max(1, len(name_tokens))) * 0.4

    return min(1.0, score)


def extract_parameters_from_query(query: str, skill: SkillManifest) -> Dict[str, Any]:
    """Extract argument values from query based on parameter names and patterns."""
    extracted = {}
    query_lower = query.lower()

    for p_name, p_info in skill.parameters.items():
        # Look for explicit patterns like 'called <value>', 'named <value>', 'in <value>'
        if p_name in ("name", "project_name", "app_name"):
            m = re.search(r"(?:called|named|name(?:d)?)\s+['\"]?([a-zA-Z0-9_\-]+)['\"]?", query, re.IGNORECASE)
            if m:
                extracted[p_name] = m.group(1)
                continue
        if p_name in ("path", "directory", "folder", "base_dir", "dest"):
            m = re.search(r"(?:in|to|at|folder)\s+['\"]?([~a-zA-Z0-9_\-\\/.]+)['\"]?", query, re.IGNORECASE)
            if m:
                extracted[p_name] = m.group(1)
                continue
        if p_name in ("cmd", "command"):
            m = re.search(r"(?:command|cmd):\s*(.+)$", query, re.IGNORECASE)
            if m:
                extracted[p_name] = m.group(1).strip()
                continue
        if p_name in ("text", "content", "note"):
            m = re.search(r"(?:remember|note|that)\s+(.+)$", query, re.IGNORECASE)
            if m:
                extracted[p_name] = m.group(1).strip()
                continue

    return extracted


def find_matching_skill(
    query: str,
    skills: List[SkillManifest],
    threshold: float = 0.35,
) -> Optional[RetrievedSkillMatch]:
    """Search registered skills for a relevant workflow."""
    best_match: Optional[RetrievedSkillMatch] = None
    best_score = 0.0

    for skill in skills:
        if not skill.is_enabled:
            continue
        score = calculate_similarity(query, skill)
        if score >= threshold and score > best_score:
            params = extract_parameters_from_query(query, skill)
            best_score = score
            best_match = RetrievedSkillMatch(
                skill=skill,
                confidence=round(score, 2),
                extracted_params=params,
                match_reason=f"Matched keywords with '{skill.name}' (confidence: {round(score * 100)}%)",
            )

    return best_match
