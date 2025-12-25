import os
import re
import shutil

TARGET_DIR = "processed_output"

def get_package_name(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        # Read first few lines to find package declaration
        for line in f:
            line = line.strip()
            if line.startswith("package "):
                # Extract package name (remove 'package ' and ending ';')
                match = re.search(r'package\s+([\w\.]+);', line)
                if match:
                    return match.group(1)
    return None

def move_files():
    count_moved = 0
    count_skipped = 0
    
    # Walk bottom-up to handle directories easier if needed, though top-down is fine for moving files
    for root, dirs, files in os.walk(TARGET_DIR):
        for file in files:
            if not file.endswith(".java"):
                continue
                
            file_path = os.path.join(root, file)
            package_name = get_package_name(file_path)
            
            if not package_name:
                print(f"Skipping {file}: No package declaration found.")
                count_skipped += 1
                continue
                
            # Convert package to path
            relative_path = package_name.replace('.', os.sep)
            new_dir = os.path.join(TARGET_DIR, relative_path)
            new_file_path = os.path.join(new_dir, file)
            
            # Check if file is already in correct place
            # os.path.normpath to handle potentially different redundant separators
            if os.path.normpath(root) == os.path.normpath(new_dir):
                continue
            
            try:
                os.makedirs(new_dir, exist_ok=True)
                shutil.move(file_path, new_file_path)
                print(f"Moved: {file} -> {relative_path}")
                count_moved += 1
            except Exception as e:
                print(f"Error moving {file}: {e}")

    # Cleanup empty directories
    print("Cleaning up empty directories...")
    for root, dirs, files in os.walk(TARGET_DIR, topdown=False):
        for dir_name in dirs:
            dir_path = os.path.join(root, dir_name)
            try:
                os.rmdir(dir_path)
                print(f"Removed empty dir: {dir_path}")
            except OSError:
                # Directory not empty
                pass

    print(f"Summary: Moved {count_moved} files. Skipped {count_skipped} files (no package found).")

if __name__ == "__main__":
    if not os.path.exists(TARGET_DIR):
        print(f"Directory {TARGET_DIR} does not exist.")
    else:
        move_files()
