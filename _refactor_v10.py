"""
Refactor script: Remove hook system, hook gate, quiz gate, old quiz/solution,
and update pipeline for v10.0
"""
import re

with open("q_animation.py", "r", encoding="utf-8") as f:
    content = f.read()

lines = content.split('\n')

# Find line indices (0-based) for sections to remove
def find_line(pattern, start=0):
    for i in range(start, len(lines)):
        if pattern in lines[i]:
            return i
    return -1

# 1. Remove Hook system: from HOOK_SYSTEM_PROMPT to just before MODULE 7.8
hook_start = find_line('HOOK_SYSTEM_PROMPT = """')
module_78 = find_line('MODULE 7.8')
# Go back to the section header line
module_78_header = module_78
while module_78_header > 0 and '═══' not in lines[module_78_header - 1]:
    module_78_header -= 1
if module_78_header > 0:
    module_78_header -= 1

print(f"Removing hook: lines {hook_start+1} to {module_78_header}")
if hook_start > 0 and module_78_header > hook_start:
    lines[hook_start:module_78_header] = []

# 2. Remove HookGate: from _HOOK_GATE_CSS to just before MODULE 7.95
hook_gate_start = find_line('_HOOK_GATE_CSS = """')
# Find the section header before it
hg_header = hook_gate_start
while hg_header > 0 and 'MODULE 7.9' not in lines[hg_header]:
    hg_header -= 1
if hg_header > 0:
    hg_header_start = hg_header
    while hg_header_start > 0 and '═══' not in lines[hg_header_start - 1]:
        hg_header_start -= 1
    if hg_header_start > 0:
        hg_header_start -= 1
    hg_header = hg_header_start

module_795 = find_line('MODULE 7.95')
hg_section_header = module_795
while hg_section_header > 0 and '═══' not in lines[hg_section_header - 1]:
    hg_section_header -= 1
if hg_section_header > 0:
    hg_section_header -= 1

print(f"Removing hook gate: lines {hg_header+1} to {hg_section_header}")
if hg_header > 0 and hg_section_header > hg_header:
    lines[hg_header:hg_section_header] = []

# 3. Remove QuizGate: from _QUIZ_GATE_HTML to just before _INLINE_QUIZ_CSS
qg_start = find_line('_QUIZ_GATE_HTML = """')
# Find section header
qg_header = qg_start
while qg_header > 0 and 'MODULE 7.95' not in lines[qg_header]:
    qg_header -= 1
if qg_header > 0:
    qg_header_start = qg_header
    while qg_header_start > 0 and '═══' not in lines[qg_header_start - 1]:
        qg_header_start -= 1
    if qg_header_start > 0:
        qg_header_start -= 1
    qg_header = qg_header_start

# Find end - just before inline quiz CSS
inline_quiz_css = find_line('_INLINE_QUIZ_CSS = """')
# go back to the comment line above it
iq_header = inline_quiz_css
while iq_header > 0 and lines[iq_header - 1].strip() != '':
    iq_header -= 1

print(f"Removing quiz gate: lines {qg_header+1} to {iq_header}")
if qg_header > 0 and iq_header > qg_header:
    lines[qg_header:iq_header] = []

# Rejoin
content = '\n'.join(lines)

# 4. Remove references to hook in pipeline function
# Replace the 4-concurrent gather call
content = content.replace(
    """    Stage 1 - Hook Animation      (AI: HookGenerator)      ┐
    Stage 2 - Concept Animation   (AI: concept engine)     ├ concurrent
    Stage 3 - Solution Animation  (AI: main engine)        │
    Stage 4 - Quiz Generation     (AI: QuizGenerator)      ┘""",
    """    Stage 1 - Concept Animation   (AI: concept engine)     ┐
    Stage 2 - Solution Animation  (AI: main engine)        ├ concurrent
    Stage 3 - Quiz Generation     (AI: QuizGenerator)      ┘"""
)

# Fix the gather call - remove HookGenerator.generate
content = content.replace(
    """        hook_html, concept_html, sol_raw, quiz_html = await asyncio.gather(
            HookGenerator.generate(question, category),
            _generate_concept_animation(question, category),
            _run_solution_ai(),
            QuizGenerator.generate(question, category),
        )""",
    """        concept_html, sol_raw, quiz_html = await asyncio.gather(
            _generate_concept_animation(question, category),
            _run_solution_ai(),
            QuizGenerator.generate(question, category),
        )"""
)

# Remove hook_animation_code from result
content = content.replace(
    '    result["hook_animation_code"]    = hook_html      # NEW\n',
    ''
)

# Remove hook from pipeline logging
content = content.replace(
    '    QAnimLogger.info("Pipeline", "Launching 4 concurrent AI stages…")',
    '    QAnimLogger.info("Pipeline", "Launching 3 concurrent AI stages…")'
)

# Remove inject_hook_gate call
content = content.replace(
    '            hook_html = inject_hook_gate(hook_html)        # ← NEW: Start Learning gate\n',
    ''
)
content = content.replace(
    '            hook_html = inject_step_controller(hook_html)  # ← NEW: manual step safety net\n',
    ''
)

# Remove inject_quiz_gate call
content = content.replace(
    '    # ── Quiz gate injection (standalone quiz_html) ───────────────────\n    quiz_html = inject_quiz_gate(quiz_html)      # ← NEW: quiz unlock gate\n',
    ''
)

# Remove hook from result render_order
content = content.replace(
    '    result["render_order"]           = ["hook_animation_code", "concept_animation_code", "animation_code", "quiz_html"]',
    '    result["render_order"]           = ["concept_animation_code", "animation_code", "quiz_html"]'
)

# Remove hook from pipeline completion log
content = content.replace(
    """        f\"hook={len(hook_html):,} \"
        f\"concept={len(concept_html):,} \"""",
    '        f"concept={len(concept_html):,} "'
)

# Remove hook from _build_failure_result
content = content.replace(
    '        "hook_animation_code":    fallback,\n',
    ''
)

# Remove hook from CLI test
content = content.replace(
    """        hook_html     = result.get("hook_animation_code", "")
        concept_html  = result.get("concept_animation_code", "")""",
    '        concept_html  = result.get("concept_animation_code", "")'
)

content = content.replace(
    '        print(f"[Stage 1] Hook      : {len(hook_html):,} chars")\n',
    ''
)

# Fix stage numbering in CLI
content = content.replace(
    'print(f"[Stage 2] Concept   : {len(concept_html):,} chars")',
    'print(f"[Stage 1] Concept   : {len(concept_html):,} chars")'
)
content = content.replace(
    'print(f"[Stage 3] Solution  : {len(solution_html):,} chars")',
    'print(f"[Stage 2] Solution  : {len(solution_html):,} chars")'
)
content = content.replace(
    'print(f"[Stage 4] Quiz      : {len(quiz_html):,} chars")',
    'print(f"[Stage 3] Quiz      : {len(quiz_html):,} chars")'
)

# Remove hook file save from CLI
hook_save_block = """        # Save Stage 1 — Hook Animation
        hook_out = f"q_anim_v70_{slug}_hook.html"
        with open(hook_out, "w", encoding="utf-8") as f:
            f.write(hook_html)
        print(f"\\n[Stage 1] Hook saved    : {hook_out}")

        # Save Stage 2 — Concept Animation (with Notes)"""
content = content.replace(hook_save_block, "        # Save Stage 1 — Concept Animation (with Notes)")

content = content.replace(
    '        concept_out = f"q_anim_v70_{slug}_concept.html"',
    '        concept_out = f"q_anim_v10_{slug}_concept.html"'
)
content = content.replace(
    '        # Save Stage 3 — Solution Animation (with Notes + ToFind + Solution)',
    '        # Save Stage 2 — Solution Animation'
)
content = content.replace(
    '        solution_out = f"q_anim_v70_{slug}_solution.html"',
    '        solution_out = f"q_anim_v10_{slug}_solution.html"'
)
content = content.replace(
    '        # Save Stage 4 — Quiz',
    '        # Save Stage 3 — Quiz'
)
content = content.replace(
    '        quiz_out = f"q_anim_v70_{slug}_quiz.html"',
    '        quiz_out = f"q_anim_v10_{slug}_quiz.html"'
)

# Fix stage labels in CLI save output
content = content.replace(
    'print(f"[Stage 2] Concept saved : {concept_out}")',
    'print(f"\\n[Stage 1] Concept saved : {concept_out}")'
)
content = content.replace(
    'print(f"[Stage 3] Solution saved: {solution_out}")',
    'print(f"[Stage 2] Solution saved: {solution_out}")'
)
content = content.replace(
    'print(f"[Stage 4] Quiz saved    : {quiz_out}\\n")',
    'print(f"[Stage 3] Quiz saved    : {quiz_out}\\n")'
)

# Update version references
content = content.replace('QAnim v9.0', 'QAnim v10.0')
content = content.replace('engine_version"]         = "v9.0"', 'engine_version"]         = "v10.0"')
content = content.replace('"engine_version":         "v9.0"', '"engine_version":         "v10.0"')

# Update step controller module header
content = content.replace(
    'Runs on: hook_html, concept_html, solution_html',
    'Runs on: concept_html, solution_html'
)

# Update pipeline docstring  
content = content.replace(
    '    FOUR-STAGE CONCURRENT PIPELINE (v7.0):',
    '    THREE-STAGE CONCURRENT PIPELINE (v10.0):'
)

with open("q_animation.py", "w", encoding="utf-8") as f:
    f.write(content)

print("✅ Refactor script completed successfully!")
print(f"Final file size: {len(content):,} bytes")
