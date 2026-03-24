import json
import os
from dataclasses import asdict, is_dataclass
from enum import Enum

try:
    import pandas as pd
    _has_pandas = True
except ImportError:
    _has_pandas = False


class EnhancedJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder to handle Enums, Dataclasses, and DataFrames."""
    def default(self, o):
        if isinstance(o, Enum):
            return o.value
        if is_dataclass(o):
            return asdict(o)
        if _has_pandas and isinstance(o, pd.DataFrame):
            return o.to_dict(orient="records")
        if _has_pandas and isinstance(o, pd.Series):
            return o.tolist()
        return super().default(o)

def save_layer_state(job, layer_num: str, layer_name: str, stage: str):
    """
    Saves the state of the Job object to a JSON file.
    Args:
        job: The Job dataclass instance.
        layer_num: Layer identifier (e.g., '1', '3b').
        layer_name: Descriptive name of the layer.
        stage: 'input' or 'output'.
    """
    try:
        # Resolve path relative to project root (assuming bpmn_pipeline is a top-level dir)
        # We use a nested folder for the job_id to keep things organized
        base_dir = os.path.join("bpmn_pipeline", "outputs", "layer-wise-output", job.job_id)
        os.makedirs(base_dir, exist_ok=True)

        filename = f"L{layer_num}_{layer_name}_{stage}.json"
        filepath = os.path.join(base_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(asdict(job), f, cls=EnhancedJSONEncoder, indent=2)
            
    except Exception as e:
        # We don't want debugging to crash the pipeline
        print(f"[Debug] Failed to save state for L{layer_num} ({stage}): {e}")
