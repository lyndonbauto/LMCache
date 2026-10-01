# SPDX-License-Identifier: Apache-2.0
# Scratch copy of the installed vLLM with only "Part 1" of vllm#49250 applied:
# GPUModelRunner.update_requests (V2) pushes a backward num_computed_tokens jump
# (a recompute after a KV load failure) to the GPU tensor. The installed vLLM is untouched.
import pathlib, shutil
SRC = pathlib.Path('/opt/python/lib/python3.14/site-packages/vllm')
DST = pathlib.Path('/work/scratch/vllm-d17fix/vllm')
if DST.parent.exists():
    shutil.rmtree(DST.parent)
shutil.copytree(SRC, DST, symlinks=True)
f = DST / 'v1/worker/gpu/model_runner.py'
s = f.read_text()
old = """            req_index = self.req_states.req_id_to_index[req_id]
            num_computed_tokens_np[req_index] = num_computed_tokens
"""
new = """            req_index = self.req_states.req_id_to_index[req_id]
            if num_computed_tokens < num_computed_tokens_np[req_index]:
                # D-17 scratch fix (vllm#49250 part 1): a recompute after a KV
                # load failure moves num_computed_tokens backward; the GPU copy
                # only moves forward otherwise.
                self.req_states.num_computed_tokens.stage_write_elem(
                    req_index, num_computed_tokens
                )
                rewound = True
            num_computed_tokens_np[req_index] = num_computed_tokens
"""
assert s.count(old) == 1, 'anchor 1'
s = s.replace(old, new)
old2 = """        num_computed_tokens_np = self.req_states.num_computed_tokens_np
        for req_id, num_computed_tokens, req_new_block_ids in zip("""
new2 = """        num_computed_tokens_np = self.req_states.num_computed_tokens_np
        rewound = False
        for req_id, num_computed_tokens, req_new_block_ids in zip("""
assert s.count(old2) == 1, 'anchor 2'
s = s.replace(old2, new2)
old3 = """        # Update CPU num_computed_prefill_tokens.
        np.minimum("""
new3 = """        if rewound:
            self.req_states.num_computed_tokens.apply_write()

        # Update CPU num_computed_prefill_tokens.
        np.minimum("""
assert s.count(old3) == 1, 'anchor 3'
s = s.replace(old3, new3)
f.write_text(s)
print('patched', f)
