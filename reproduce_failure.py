import sys
import os
import json
from datetime import datetime

# Add the project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "bpmn_pipeline")))

from models.schemas import Job, JobStatus, BPMNNodeType
from pipeline.orchestrator import LAYERS

def reproduce():
    source_file = "/Users/prayagraj/Documents/kaignetic/test.pdf"
    if not os.path.exists(source_file):
        source_file = "/Users/prayagraj/Documents/kaignetic/prd.pdf"
    
    print(f"Using source file: {source_file}")
    
    job = Job(
        job_id="repro_job",
        source_file_path=source_file,
        created_at=datetime.utcnow().isoformat(),
    )

    for layer_num, layer_mod in LAYERS:
        print(f"Running L{layer_num}: {layer_mod.__name__}")
        try:
            layer_mod.run(job)
            layer_mod.validate_gate(job)
        except Exception as e:
            # Handle soft gate failures
            soft_types = getattr(layer_mod, "SoftGateFailure", None)
            if soft_types and isinstance(e, soft_types):
                print(f"Soft failure at L{layer_num}: {e}")
                continue
            
            print(f"Failed at L{layer_num}: {e}")
            
            # Inspect processes
            for proc in job.processes:
                if "8.3" in proc.name or "BGV" in proc.name:
                    print(f"\n--- Process: {proc.name} ---")
                    unreachable = [n.node_id for n in proc.bpmn_nodes if getattr(n, "unreachable_from_start", False)]
                    print(f"Nodes: {len(proc.bpmn_nodes)}")
                    print(f"Unreachable ({len(unreachable)}): {unreachable}")
                    
                    # Print nodes and edges for this process
                    node_id_to_label = {n.node_id: (n.label or n.bpmn_type.value) for n in proc.bpmn_nodes}
                    print("\nEdges:")
                    for e in proc.bpmn_edges:
                        print(f"  {node_id_to_label.get(e.source_node_id, e.source_node_id)} -> {node_id_to_label.get(e.target_node_id, e.target_node_id)} [{e.label or ''}]")
                    
                    print("\nUnreachable details:")
                    for nid in unreachable:
                        node = next(n for n in proc.bpmn_nodes if n.node_id == nid)
                        print(f"  ID: {nid}, Type: {node.bpmn_type}, Label: {node.label}")

            return # Stop after first failure

if __name__ == "__main__":
    reproduce()
