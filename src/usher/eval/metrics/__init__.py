"""IR scoring. `ir.py` is the only module in this project that imports `ranx`.

The confinement is the mitigation for the one risk the design records against
adopting it: `numba`/`llvmlite` pin an LLVM ABI and historically lag new
CPython, so a 3.14 move could block this extra. Swapping to `ir_measures`
(4 packages) is then one file.
"""
