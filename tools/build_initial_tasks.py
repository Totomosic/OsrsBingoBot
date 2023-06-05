import argparse
import json

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=str, required=True, help="Filename of output tasks json file")
    parser.add_argument("--file", type=str, action="append", help="Path to one of the original format task files")
    args = parser.parse_args()

    tasks = []
    task_id = 1
    if args.file:
        for file in args.file:
            with open(file, "r", encoding="utf-8") as f:
                file_data = f.readlines()
            for line in file_data:
                tasks.append({
                    "id": task_id,
                    "weight": 1,
                    "task": line.strip(),
                })
                task_id += 1

    with open(args.output, "w") as f:
        json.dump({
            "tasks": tasks,
        }, f)

if __name__ == "__main__":
    main()
