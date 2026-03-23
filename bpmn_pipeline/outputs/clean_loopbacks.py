import re
import sys
import os

def clean_loopbacks(filepath):
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        return

    print(f"Cleaning redundant loopbacks in {filepath}...")

    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # This regex matches one or more occurrences of " [loop-back]" 
    # and replaces them with a single " [loop-back]"
    cleaned_content = re.sub(r'(\s*\[loop-back\])+', r' [loop-back]', content)

    # Save the cleaned content back
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(cleaned_content)
        
    print(f"Successfully cleaned: {filepath}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python clean_loopbacks.py <path_to_bpmn_file>")
        print("Example: python clean_loopbacks.py 086c7ac2-d16.bpmn")
    else:
        file_to_clean = sys.argv[1]
        clean_loopbacks(file_to_clean)
