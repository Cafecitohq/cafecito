import textwrap

from cafecito.spans import go_spans, js_spans, symbol_spans
from cafecito.writeset import _attribute_lang

TS = textwrap.dedent("""\
    import { thing } from "./thing";

    export function greet(name: string): string {
      return `hello ${name}`;
    }

    export const shout = (name: string) => {
      // braces in comments { should not } count
      return name.toUpperCase();
    };

    export class Greeter {
      private prefix = "hi";

      constructor(prefix: string) {
        this.prefix = prefix;
      }

      async greetAll(names: string[]) {
        return names.map((n) => {
          return `${this.prefix} ${n}`;
        });
      }

      static of(prefix: string) {
        return new Greeter(prefix);
      }
    }

    interface Options {
      loud: boolean;
    }
    """)

GO = textwrap.dedent("""\
    package brew

    import "fmt"

    type Kettle struct {
        Temp int
    }

    func Boil(k *Kettle) error {
        if k.Temp > 100 {
            return fmt.Errorf("too hot: %d", k.Temp)
        }
        return nil
    }

    func (k *Kettle) Pour(cups int) string {
        s := "pouring {not a brace}"
        return s
    }
    """)


def _by_name(spans):
    return {name: (start, end) for name, start, end in spans}


def test_ts_top_level_functions_and_arrows():
    spans = _by_name(js_spans(TS))
    assert "greet" in spans and spans["greet"][0] == 3
    assert "shout" in spans
    # the arrow body's braces close the span
    assert spans["shout"][1] > spans["shout"][0]


def test_ts_class_and_methods():
    spans = _by_name(js_spans(TS))
    assert "Greeter" in spans
    assert "Greeter.greetAll" in spans
    assert "Greeter.of" in spans
    gs, ge = spans["Greeter"]
    ms, me = spans["Greeter.greetAll"]
    assert gs < ms and me <= ge  # method nested inside class span


def test_ts_interface_span():
    spans = _by_name(js_spans(TS))
    assert "Options" in spans


def test_ts_nested_callback_attributes_to_method():
    spans = js_spans(TS)
    # the line inside names.map(...) callback
    line = next(i for i, l in enumerate(TS.splitlines(), 1)
                if "this.prefix} ${n}" in l)
    got = _attribute_lang("a.ts", [(line, line)], spans, "ts")
    assert got == {"ts:a.ts::Greeter.greetAll"}


def test_ts_module_level_line():
    spans = js_spans(TS)
    got = _attribute_lang("a.ts", [(1, 1)], spans, "ts")
    assert got == {"ts:a.ts::<module>"}


def test_js_unbalanced_returns_none():
    assert js_spans("function broken() {\n  if (x) {\n}\n") is None


def test_go_functions_methods_types():
    spans = _by_name(go_spans(GO))
    assert "Kettle" in spans
    assert "Boil" in spans
    assert "Kettle.Pour" in spans


def test_go_braces_in_strings_ignored():
    spans = _by_name(go_spans(GO))
    s, e = spans["Kettle.Pour"]
    assert e >= s + 2  # span survives the "{not a brace}" literal


def test_go_attribution():
    spans = go_spans(GO)
    line = next(i for i, l in enumerate(GO.splitlines(), 1)
                if l.strip().startswith("if k.Temp"))
    got = _attribute_lang("brew/kettle.go", [(line, line)], spans, "go")
    assert got == {"go:brew/kettle.go::Boil"}


def test_symbol_spans_dispatch():
    assert symbol_spans("func A() {\n}\n", "go") is not None
    assert symbol_spans("function a() {\n}\n", "js") is not None
    assert symbol_spans("anything", "cobol") is None
