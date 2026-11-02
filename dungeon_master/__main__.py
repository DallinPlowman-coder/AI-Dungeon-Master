import argparse

from .config import Settings
from .embeddings import LocalEmbeddings


def main():
    parser = argparse.ArgumentParser(description="AI Dungeon Master book library")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--prepare-model", action="store_true", help="Download and verify the local embedding model")
    args = parser.parse_args()
    if args.prepare_model:
        embeddings = LocalEmbeddings(Settings.from_env())
        vector = embeddings.query("A hero enters an ancient dungeon.")
        print(f"Ready: {embeddings.name} ({len(vector)} dimensions)")
    else:
        import uvicorn
        uvicorn.run("dungeon_master.main:app", host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
