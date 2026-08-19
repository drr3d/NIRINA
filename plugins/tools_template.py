from core_agent.registry import ToolRegistry

@ToolRegistry.register(category="safe")
def hi_there(name: str) -> str:
    """
        This  example docstring that agent will read.
        so make it clear.

        This tools intended to say hi to who ever `name`
        you must says hi politly to user `name`
    """

    return (
            f"Your Master name is {name}\n"
            "give em warm hugh will you"
        )