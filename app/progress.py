class ProgressWindow:
    def __init__(self, title: str = "自動化進捗"):
        self.title = title
        self._closed = False

    def start(self) -> None:
        return

    def update(self, message: str) -> None:
        if self._closed:
            return
        print(message)

    def run(self) -> None:
        return

    def close(self) -> None:
        self._closed = True
