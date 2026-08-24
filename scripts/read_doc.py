with open(r'C:\Users\sogreatsg\.gemini\antigravity-cli\brain\8bbdd893-b52c-47b5-b63c-fb7dbc47d8cd\.system_generated\steps\3104\content.md', 'r', encoding='utf-8') as f:
    text = f.read()

import re
matches = [m.start() for m in re.finditer(r'deletemessage', text, re.IGNORECASE)]
print(f"Found {len(matches)} occurrences")
for pos in matches:
    snippet = text[pos-50:pos+300]
    if '<h4>' in snippet or 'Use this method to delete' in snippet:
        print("MATCH:")
        print(text[pos-50:pos+1500])
        print("="*60)
