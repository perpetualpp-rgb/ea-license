"""Assemble the claude.ai desk page: template + COT card + engine.js + journal.js + plan.js -> orderflow-desk.html."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def read(*p):
    with open(os.path.join(HERE, *p), encoding="utf-8") as f:
        return f.read()


def build():
    page = read("template.html")
    page = page.replace("<!--COT_SECTION-->", read("cot_section.html").rstrip("\n"))
    page = page.replace("<!--ENGINE-->", "<script>\n" + read("..", "web", "engine.js") + "\n</script>")
    page = page.replace("<!--JOURNAL-->", "<script>\n" + read("journal.js") + "\n</script>")
    page = page.replace("<!--PLAN-->", "<script>\n" + read("plan.js") + "\n</script>")
    page += "\n" + read("cot_script.html")
    with open(os.path.join(HERE, "orderflow-desk.html"), "w", encoding="utf-8") as f:
        f.write(page)
    return page


if __name__ == "__main__":
    print(len(build()), "bytes -> orderflow-desk.html")
