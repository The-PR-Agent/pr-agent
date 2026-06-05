---
title: "Example Best Practice"
sidebar_position: 8
---

## Recommend Python Best Practices

This document outlines a series of recommended best practices for Python development. These guidelines aim to improve code quality, maintainability, and readability.

### Imports

Use  `import`  statements for packages and modules only, not for individual types, classes, or functions.

#### Definition

Reusability mechanism for sharing code from one module to another.

#### Decision

- Use  `import x`  for importing packages and modules.
- Use  `from x import y`  where  `x`  is the package prefix and  `y`  is the module name with no prefix.
- Use  `from x import y as z`  in any of the following circumstances:
  - Two modules named  `y`  are to be imported.
  - `y`  conflicts with a top-level name defined in the current module.
  - `y`  conflicts with a common parameter name that is part of the public API (e.g.,  `features`).
  - `y`  is an inconveniently long name, or too generic in the context of your code
- Use  `import y as z`  only when  `z`  is a standard abbreviation (e.g.,  `import numpy as np`).

For example the module  `sound.effects.echo`  may be imported as follows:

```
from sound.effects import echo
...
echo.EchoFilter(input, output, delay=0.7, atten=4)

```

Do not use relative names in imports. Even if the module is in the same package, use the full package name. This helps prevent unintentionally importing a package twice.

##### Exemptions

Exemptions from this rule:

- Symbols from the following modules are used to support static analysis and type checking:
  - [`typing`  module](https://google.github.io/styleguide/pyguide.html#typing-imports)
  - [`collections.abc`  module](https://google.github.io/styleguide/pyguide.html#typing-imports)
  - [`typing_extensions`  module](https://github.com/python/typing_extensions/blob/main/README.md)
- Redirects from the  [six.moves module](https://six.readthedocs.io/#module-six.moves).

### Packages

Import each module using the full pathname location of the module.

#### Decision

All new code should import each module by its full package name.

Imports should be as follows:

```
Yes:
  # Reference absl.flags in code with the complete name (verbose).
  import absl.flags
  from doctor.who import jodie

  _FOO = absl.flags.DEFINE_string(...)

```

```
Yes:
  # Reference flags in code with just the module name (common).
  from absl import flags
  from doctor.who import jodie

  _FOO = flags.DEFINE_string(...)

```

_(assume this file lives in  `doctor/who/`  where  `jodie.py`  also exists)_

```
No:
  # Unclear what module the author wanted and what will be imported.  The actual
  # import behavior depends on external factors controlling sys.path.
  # Which possible jodie module did the author intend to import?
  import jodie

```

The directory the main binary is located in should not be assumed to be in  `sys.path`  despite that happening in some environments. This being the case, code should assume that  `import jodie`  refers to a third-party or top-level package named  `jodie`, not a local  `jodie.py`.

### Default Iterators and Operators

Use default iterators and operators for types that support them, like lists, dictionaries, and files.

#### Definition

Container types, like dictionaries and lists, define default iterators and membership test operators (“in” and “not in”).

#### Decision

Use default iterators and operators for types that support them, like lists, dictionaries, and files. The built-in types define iterator methods, too. Prefer these methods to methods that return lists, except that you should not mutate a container while iterating over it.

```
Yes:  for key in adict: ...
      if obj in alist: ...
      for line in afile: ...
      for k, v in adict.items(): ...
```

```
No:   for key in adict.keys(): ...
      for line in afile.readlines(): ...
```

### Lambda Functions

Okay for one-liners. Prefer generator expressions over  `map()`  or  `filter()`  with a  `lambda`.

#### Decision

Lambdas are allowed. If the code inside the lambda function spans multiple lines or is longer than 60-80 chars, it might be better to define it as a regular  [nested function](https://google.github.io/styleguide/pyguide.html#lexical-scoping).

For common operations like multiplication, use the functions from the  `operator`  module instead of lambda functions. For example, prefer  `operator.mul`  to  `lambda x, y: x * y`.

### Default Argument Values

Okay in most cases.

#### Definition

You can specify values for variables at the end of a function’s parameter list, e.g.,  `def foo(a, b=0):`. If  `foo`  is called with only one argument,  `b`  is set to 0. If it is called with two arguments,  `b`  has the value of the second argument.

#### Decision

Okay to use with the following caveat:

Do not use mutable objects as default values in the function or method definition.

```
Yes: def foo(a, b=None):
         if b is None:
             b = []
Yes: def foo(a, b: Sequence | None = None):
         if b is None:
             b = []
Yes: def foo(a, b: Sequence = ()):  # Empty tuple OK since tuples are immutable.
         ...
```

```
from absl import flags
_FOO = flags.DEFINE_string(...)

No:  def foo(a, b=[]):
         ...
No:  def foo(a, b=time.time()):  # Is `b` supposed to represent when this module was loaded?
         ...
No:  def foo(a, b=_FOO.value):  # sys.argv has not yet been parsed...
         ...
No:  def foo(a, b: Mapping = {}):  # Could still get passed to unchecked code.
         ...
```

### True/False Evaluations

Use the “implicit” false if possible, e.g.,  `if foo:`  rather than  `if foo != []:`

### Lexical Scoping

Okay to use.

An example of the use of this feature is:

```
def get_adder(summand1: float) -> Callable[[float], float]:
    """Returns a function that adds numbers to a given number."""
    def adder(summand2: float) -> float:
        return summand1 + summand2

    return adder
```

#### Decision

Okay to use.

### Threading

Do not rely on the atomicity of built-in types.

While Python’s built-in data types such as dictionaries appear to have atomic operations, there are corner cases where they aren’t atomic (e.g. if  `__hash__`  or  `__eq__`  are implemented as Python methods) and their atomicity should not be relied upon. Neither should you rely on atomic variable assignment (since this in turn depends on dictionaries).

Use the  `queue`  module’s  `Queue`  data type as the preferred way to communicate data between threads. Otherwise, use the  `threading`  module and its locking primitives. Prefer condition variables and  `threading.Condition`  instead of using lower-level locks.
