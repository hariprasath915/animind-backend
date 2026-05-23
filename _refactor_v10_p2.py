"""
Phase 2 refactor: Replace old quiz system with new modules.
Uses simple string concatenation to avoid f-string nesting issues.
"""
import json

with open("q_animation.py", "r", encoding="utf-8") as f:
    content = f.read()

# Find boundaries
eq_line = "=" * 70

old_start = content.find("#  MODULE 7.6")
# Go back to the section delimiter
section_start = content.rfind("# " + eq_line, 0, old_start)

module_78 = content.find("MODULE 7.8")
section_78_start = content.rfind("# " + eq_line, 0, module_78)

print(f"Section 7.6 starts at char: {section_start}")
print(f"Section 7.8 starts at char: {section_78_start}")

if section_start == -1 or section_78_start == -1:
    print("ERROR: Could not find boundaries")
    exit(1)

# Read the new module code from a separate file
NEW_CODE = open("_new_modules.py", "r", encoding="utf-8").read()

content = content[:section_start] + NEW_CODE + "\n\n" + content[section_78_start:]

with open("q_animation.py", "w", encoding="utf-8") as f:
    f.write(content)

print(f"Done. New file size: {len(content)} chars")
