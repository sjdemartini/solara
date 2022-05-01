import contextlib
import importlib
import inspect
import logging
import os
import sys
import threading
from typing import Callable, Dict, List, Set

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger("solara.server.reload")


class Watcher(FileSystemEventHandler):
    def __init__(self, files, on_change: Callable[[str], None]):
        self.on_change = on_change
        self.observers: List[Observer] = []
        self.directories: Set[str] = set()
        self.files: List[str] = []
        self.mtimes: Dict[str, float] = dict()
        for file in files:
            self.add_file(file)

    def close(self):
        for o in self.observers:
            o.unschedule_all()
            o.stop()
        # this makes testing slow
        # for o in self.observers:
        #     o.join()

    def add_file(self, file):
        file = os.path.abspath(os.path.realpath(file))
        if file not in self.files:
            logger.info("Watching file %s", file)
            if file in self.files:
                raise RuntimeError(f"{file} was already added")
            self.files.append(file)
            self._watch_file(file)

    def _watch_file(self, file):
        self.mtimes[file] = os.path.getmtime(file)
        directory = os.path.realpath(os.path.dirname(file))
        self.watch_directory(directory)

    def watch_directory(self, directory):
        if directory not in self.directories:
            logger.debug("Watching directory %s", directory)
            observer = Observer()
            observer.schedule(self, directory, recursive=False)
            observer.start()
            self.observers.append(observer)
            self.directories.add(directory)

    def on_modified(self, event):
        super(Watcher, self).on_modified(event)
        logger.info("Watch event: %s", event)
        print("EVENT", event)
        if not event.is_directory:
            if event.src_path in self.files:
                mtime_new = os.path.getmtime(event.src_path)
                changed = mtime_new > self.mtimes[event.src_path]
                self.mtimes[event.src_path] = mtime_new
                if changed:
                    logger.debug("File modified: %s", event.src_path)
                    try:
                        self.on_change(event.src_path)
                    except:  # noqa
                        # we are in the watchdog thread here, all we can do is report
                        # and continue running (otherwise reload stops working)
                        logger.exception("Error while executing on change handler")
                else:
                    logger.debug("File reported modified, but mtime did not change: %s", event.src_path)
                    print("File reported modified, but mtime did not change: ", event.src_path)
            else:
                logger.debug("Ignore file modification: %s", event.src_path)


class Reloader:
    def __init__(self, on_change: Callable[[str], None] = None) -> None:
        self.watched_modules: Set[str] = set()
        self.on_change = on_change
        self.watcher = Watcher([], self._on_change)
        self.requires_reload = False
        self.reload_event_next = threading.Event()

    def _on_change(self, name):
        # used for testing
        self.reload_event_next.set()
        self.reload_event_next.clear()
        # flag that we need to reload all modules next time
        self.requires_reload = True
        # and forward callback
        if self.on_change:
            self.on_change(name)

    def close(self):
        self.watcher.close()

    def reload(self):
        logger.info("Reloading modules... %s", self.watched_modules)
        # not sure if this is needed
        importlib.invalidate_caches()
        for mod in self.watched_modules:
            # it could be that a second run does not import the module
            # so we should check if it is imported
            if mod in sys.modules:
                del sys.modules[mod]
        # if all succesfull...
        self.requires_reload = False

    @contextlib.contextmanager
    def watch(self):
        """Use this context manager to execute code so that we can track loaded modules and reload them when needed"""
        if self.requires_reload:
            self.reload()

        modules_before = set(sys.modules)
        try:
            yield
        finally:
            modules_after = set(sys.modules)
            # these are imported during the 'yield'
            modules_added = modules_after - modules_before
            modules_to_consider = modules_added - set(self.watched_modules)
            # TODO: libraries that solara uses need special care
            # modules_always = {k for k in sys.modules if k.startswith("react_ipywidgets")}
            # modules_to_consider = modules_to_consider | modules_always
            modules_watching = set()
            if modules_to_consider:
                logger.debug("Found modules %s", modules_to_consider)
            for modname in modules_to_consider:
                module = sys.modules[modname]
                path = None
                try:
                    path = inspect.getfile(module)
                except Exception:
                    pass
                if path:
                    if not path.startswith(sys.prefix):
                        self.watcher.add_file(path)
                        self.watched_modules.add(modname)
                        modules_watching.add(modname)
            if modules_watching:
                logger.info("Watching modules: %s for reload", modules_watching)


# there is only a reloader, and there should be only 1 app
# that connect to the on_change
reloader = Reloader()