import json
import sys
from .evaluator import evaluate
if __name__=='__main__':
    payload=json.load(sys.stdin)
    result=evaluate(payload)
    sys.stdout.write(json.dumps(result,allow_nan=False))
