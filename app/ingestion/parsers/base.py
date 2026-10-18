from abc import ABC, abstractmethod

class Parser(ABC):
    @abstractmethod
    def parse(self, raw_bytes: bytes) -> str:
        """Converts the file content to text."""