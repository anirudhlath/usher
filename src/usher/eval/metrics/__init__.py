"""IR scoring. `ir.py` is the only module in this project that imports `ranx`.

The confinement is the mitigation for the one risk the design records against
adopting it: `numba`/`llvmlite` pin an LLVM ABI and historically lag new
CPython, so a 3.14 move could block this extra. Swapping to `ir_measures`
(4 packages) is then one file.

**That first sentence was conventional until 2026-08-19 and is now checked**,
because a mitigation nobody enforces is one import away from not existing. The
twelfth import contract forbids `ranx` everywhere in `usher` except this
package -- measured: before it, a *used* `import ranx` in `usher/adapters/http.py`
reported 11 kept, 0 broken. A `forbidden` contract's sources cover a module and
all its descendants, so it cannot see *inside* this package; the last inch, that
`ir.py` is the only module here that names the library, is held by
`tests/unit/test_eval_contract.py::test_only_the_ir_module_inside_the_metrics_package_names_ranx`.
"""
