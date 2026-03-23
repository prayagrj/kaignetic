import json
from models.schemas import Job
from pipeline.orchestrator import LAYERS

def debug_run():
    job = Job(job_id="test", source_file_path="/Users/prayagraj/Documents/kaignetic/test.pdf")
    # Run up to layer 4
    for num, mod in LAYERS:
        print(f"Running L{num}")
        mod.run(job)
        if num == 4:
            break

    print("\n--- L4 Context Index ---")
    if job.context_index:
        actors = job.context_index.actor_registry.actors if job.context_index.actor_registry else []
        print(f"Actors ({len(actors)}):")
        for a in actors:
            print(f"  - {a.canonical_name} (aliases: {a.aliases})")
        
        glossary = job.context_index.glossary or []
        print(f"\nGlossary ({len(glossary)}):")
        for g in glossary:
            print(f"  - {g.term}: {g.definition[:50]}...")
    else:
        print("No context index found!")

if __name__ == "__main__":
    debug_run()
