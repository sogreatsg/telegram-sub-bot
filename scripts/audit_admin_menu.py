import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
admin_file = (BASE_DIR / "bot" / "handlers" / "admin.py").read_text(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

# Find all commands registered in router
cmd_matches = re.findall(r'@router\.message\(Command\((.*?)\)', admin_file)
all_handlers_cmds = []
for c in cmd_matches:
    names = [s.strip().strip('"\'') for s in c.split(",") if not s.strip().startswith("prefix") and not s.strip().startswith("ignore") and s.strip()]
    if names:
        all_handlers_cmds.append(names)

print("=== ALL REGISTERED COMMANDS IN admin.py ===")
registered_set = set()
for names in all_handlers_cmds:
    registered_set.update(names)
    print("•", " / ".join([f"/{n}" for n in names]))

# Find admin menu text
menu_match = re.search(r"admin_menu_text = \((.*?)\n    \)", admin_file, re.DOTALL)
if menu_match:
    menu_text = menu_match.group(1)
    menu_cmds = set(re.findall(r"/([a-zA-Z0-9_]+)", menu_text))
    
    print("\n=== COMMANDS FOUND IN /admin MENU ===")
    for cmd in sorted(menu_cmds):
        print(f"• /{cmd}")

    missing_in_menu = []
    for names in all_handlers_cmds:
        # Check if at least one alias of this handler is in menu
        if not any(alias in menu_cmds for alias in names):
            missing_in_menu.append(names)

    print("\n=== AUDIT RESULTS ===")
    if missing_in_menu:
        print(f"⚠️ FOUND {len(missing_in_menu)} HANDLER(S) MISSING IN /admin MENU:")
        for names in missing_in_menu:
            print("  ❌", " / ".join([f"/{n}" for n in names]))
    else:
        print("✅ ALL registered admin command handlers are documented in /admin menu!")
