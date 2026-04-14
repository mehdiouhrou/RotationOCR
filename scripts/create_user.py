#!/usr/bin/env python3
import json
import sys
from pathlib import Path

import bcrypt


def main():
    if len(sys.argv) != 3:
        print("Usage: python3 scripts/create_user.py <username> <password>")
        sys.exit(1)

    username = sys.argv[1].strip()
    password = sys.argv[2]
    if not username or not password:
        print("Username and password are required.")
        sys.exit(1)

    users_file = Path(__file__).resolve().parent.parent / "users.json"
    if users_file.exists():
        data = json.loads(users_file.read_text(encoding="utf-8"))
    else:
        data = []

    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    updated = False
    for item in data:
        if item.get("username") == username:
            item["password"] = password_hash
            updated = True
            break

    if not updated:
        data.append({"username": username, "password": password_hash})

    users_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"User '{username}' saved in {users_file}")


if __name__ == "__main__":
    main()
