"""LCD status display, behind an abstraction so the exact panel (character
HD44780-over-I2C, small SPI TFT via luma.lcd, etc.) is a config choice, not
a code fork.
"""

from abc import ABC, abstractmethod


class Display(ABC):
    @abstractmethod
    def show_status(self, line1: str, line2: str = "") -> None:
        """Show now-playing / status text. Exact line count and wrapping is
        panel-dependent; implementations should truncate/scroll as needed.
        """

    @abstractmethod
    def clear(self) -> None:
        ...


class NullDisplay(Display):
    """No-op display for development without a panel attached."""

    def show_status(self, line1: str, line2: str = "") -> None:
        print(f"[display] {line1} | {line2}")

    def clear(self) -> None:
        pass
