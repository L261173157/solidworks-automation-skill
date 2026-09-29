"""Fake 对象体系: 模拟 SolidWorks 对象树中库代码真正触达的表面。

契约锚点: tests/solidworks_errorcheck_regression.py (真机 5 场景) 与
tests/solidworks_week3_delivery_regression.py 的真实调用序列。
"""
from __future__ import annotations

from pathlib import Path

from .com import ComJournal

PLANE_NAME_HINTS = ("plane", "基准面")


class FakeSketchSegment:
    def __init__(self, name: str, journal: ComJournal):
        self.name = name
        self._journal = journal

    def Select2(self, append=False, mark=0):
        self._journal.record("SketchSegment", "Select2", append, mark)
        return True

    Select4 = Select2
    Select3 = Select2


class FakeSketch:
    def __init__(self, name: str, journal: ComJournal):
        self.Name = name
        self._journal = journal
        self.segments: list[FakeSketchSegment] = []

    def GetFeature(self):
        return None

    def GetSketchContours(self):
        return ()

    def GetSketchRegions(self):
        return ()

    def GetSketchSegments(self):
        return tuple(self.segments)

    def Select2(self, append=False, mark=0):
        self._journal.record("Sketch", "Select2", append, mark)
        return True

    Select4 = Select2


class FakeSketchManager:
    def __init__(self, model, journal: ComJournal):
        self._model = model
        self._journal = journal
        self.ActiveSketch = None
        self._counter = 0

    def InsertSketch(self, flag):
        self._journal.record("SketchManager", "InsertSketch", flag)
        if self.ActiveSketch is None:
            self._counter += 1
            self.ActiveSketch = FakeSketch(f"Sketch{self._counter}", self._journal)
            self._model.sketches.append(self.ActiveSketch)
        else:
            self.ActiveSketch = None
        return True

    def _add_segments(self, count):
        segments = []
        if self.ActiveSketch is None:
            self.InsertSketch(True)
        for index in range(count):
            self._counter += 1
            segment = FakeSketchSegment(f"Line{self._counter}", self._journal)
            self.ActiveSketch.segments.append(segment)
            segments.append(segment)
        return segments

    def CreateCornerRectangle(self, x1, y1, z1, x2, y2, z2):
        self._journal.record("SketchManager", "CreateCornerRectangle", x1, y1, z1, x2, y2, z2)
        return self._add_segments(4)

    def CreateLine(self, x1, y1, z1, x2, y2, z2):
        self._journal.record("SketchManager", "CreateLine", x1, y1, z1, x2, y2, z2)
        return self._add_segments(1)

    def CreateCircleByRadius(self, cx, cy, cz, radius):
        self._journal.record("SketchManager", "CreateCircleByRadius", cx, cy, cz, radius)
        return self._add_segments(1)


class FakeFeature:
    def __init__(self, name: str, journal: ComJournal, type_name="ProfileFeature", sketch=None):
        self.Name = name
        self._journal = journal
        self._type_name = type_name
        self._sketch = sketch

    def GetTypeName2(self):
        return self._type_name

    def GetErrorCode(self):
        return 0

    def GetSpecificFeature2(self):
        return self._sketch

    def Select2(self, append=False, mark=0):
        self._journal.record("Feature", "Select2", append, mark)
        return True

    Select4 = Select2


class FakeFeatureManager:
    def __init__(self, model, journal: ComJournal):
        self._model = model
        self._journal = journal

    def FeatureExtrusion3(self, *args):
        self._journal.record("FeatureManager", "FeatureExtrusion3", *args)
        counter = self._model.next_feature_index("Boss-Extrude")
        feature = FakeFeature(f"Boss-Extrude{counter}", self._journal, "ProfileFeature")
        self._model.features.append(feature)
        return feature


class FakeExtension:
    def __init__(self, model, journal: ComJournal):
        self._model = model
        self._journal = journal

    def SelectByID2(self, name, entity_type, x, y, z, append, mark, callout, options):
        self._journal.record("Extension", "SelectByID2", name, entity_type, append, mark)
        return True

    def SaveAs(self, path, version, options, data, errors, warnings):
        self._journal.record("Extension", "SaveAs", str(path))
        Path(path).write_bytes(b"fake solidworks document")
        self._model.GetPathName = str(path)
        self._model.GetTitle = Path(path).name
        if hasattr(errors, "value"):
            errors.value = 0
        if hasattr(warnings, "value"):
            warnings.value = 0
        return True

    def Select4(self, *args):
        self._journal.record("Extension", "Select4", *args)
        return True


class FakeSelectionManager:
    def __init__(self, journal: ComJournal):
        self._journal = journal

    def GetSelectedObjectCount2(self, mark):
        self._journal.record("SelectionManager", "GetSelectedObjectCount2", mark)
        return 0


class FakeModelDoc:
    def __init__(self, app, doc_type: str, journal: ComJournal, path: str = ""):
        self._app = app
        self.doc_type = doc_type  # part | assembly | drawing
        self._journal = journal
        self.Extension = FakeExtension(self, journal)
        self.SketchManager = FakeSketchManager(self, journal)
        self.FeatureManager = FakeFeatureManager(self, journal)
        self.SelectionManager = FakeSelectionManager(journal)
        self.GetTitle = "未命名"
        self.GetPathName = path
        self.features: list[FakeFeature] = []
        self.sketches: list[FakeSketch] = []
        self.components: list["FakeComponent"] = []
        self._feature_counters: dict[str, int] = {}

    def next_feature_index(self, prefix: str) -> int:
        count = self._feature_counters.get(prefix, 0) + 1
        self._feature_counters[prefix] = count
        return count

    # --- 文档级 API ---
    def ClearSelection2(self, flag):
        self._journal.record("ModelDoc", "ClearSelection2", flag)

    def ForceRebuild3(self, flag):
        self._journal.record("ModelDoc", "ForceRebuild3", flag)
        return True

    def Save3(self, options, errors, warnings):
        self._journal.record("ModelDoc", "Save3", options)
        if hasattr(errors, "value"):
            errors.value = 0
        if hasattr(warnings, "value"):
            warnings.value = 0
        return True

    def FeatureByName(self, name):
        for feature in self.features:
            if feature.Name == name:
                return feature
        return None

    def GetFirstFeature(self):
        self._journal.record("ModelDoc", "GetFirstFeature")
        self._iter_index = 0
        return self.features[0] if self.features else None

    def GetNextFeature(self, feature):
        self._iter_index = self.features.index(feature) + 1
        if self._iter_index < len(self.features):
            return self.features[self._iter_index]
        return None

    def GetComponents(self, toplevel):
        self._journal.record("ModelDoc", "GetComponents", toplevel)
        return tuple(self.components)

    def AddComponent5(self, template, flags, x, y, z, name, path):
        self._journal.record("ModelDoc", "AddComponent5", name, x, y, z)
        return self._add_component(path)

    def AddComponent4(self, template, flags, x, y, z, path):
        self._journal.record("ModelDoc", "AddComponent4", x, y, z)
        return self._add_component(path)

    def _add_component(self, path):
        component = FakeComponent(Path(path).name if path else "组件", path, self._journal)
        self.components.append(component)
        return component

    # --- 草图/特征命名 (真机契约: current_sketch_name 依赖 ActiveSketch.Name) ---
    GetEntityCount = 0


class FakeComponent:
    def __init__(self, name: str, path: str, journal: ComJournal):
        self.Name2 = name
        self.Name = name
        self._path = path
        self._journal = journal

    def GetPathName(self):
        return self._path

    def Select4(self, append=False, mark=0):
        self._journal.record("Component", "Select4", append, mark)
        return True

    Select2 = Select4


class FakeSwApp:
    """伪 SldWorks.Application: 记录全部调用, 按需发放文档对象。"""

    def __init__(self, journal: ComJournal, revision: str = "32.5.0"):
        self._journal = journal
        self.RevisionNumber = revision
        self.Visible = True
        self.documents: list[FakeModelDoc] = []
        self._active: FakeModelDoc | None = None

    # --- 应用级 ---
    def GetUserPreferenceStringValue(self, pref_id):
        self._journal.record("SwApp", "GetUserPreferenceStringValue", pref_id)
        return ""

    def NewDocument(self, template, paper_size, width, height):
        self._journal.record("SwApp", "NewDocument", template)
        doc_type = "part"
        suffix = Path(template or "").suffix.lower()
        if suffix == ".asmdot":
            doc_type = "assembly"
        elif suffix == ".drwdot":
            doc_type = "drawing"
        model = FakeModelDoc(self, doc_type, self._journal)
        self.documents.append(model)
        self._active = model
        return model

    def OpenDoc6(self, path, doc_type, options, config, errors, warnings):
        self._journal.record("SwApp", "OpenDoc6", str(path), doc_type, options)
        target = Path(path)
        if hasattr(errors, "value"):
            errors.value = 0
        if hasattr(warnings, "value"):
            warnings.value = 0
        existing = next((doc for doc in self.documents if doc.GetPathName == str(target)), None)
        if existing is not None:
            self._active = existing
            return existing
        model = FakeModelDoc(self, {1: "part", 2: "assembly", 3: "drawing"}.get(doc_type, "part"), self._journal, str(target))
        model.GetTitle = target.name
        if not target.is_file():
            target.write_bytes(b"fake solidworks document")
        self.documents.append(model)
        self._active = model
        return model

    def CloseDoc(self, title):
        self._journal.record("SwApp", "CloseDoc", title)
        return 1

    def CloseAllDocuments(self, save_changes):
        self._journal.record("SwApp", "CloseAllDocuments", save_changes)
        self.documents.clear()
        self._active = None
        return True

    def ExitApp(self):
        self._journal.record("SwApp", "ExitApp")
        return True

    Quit = ExitApp

    @property
    def ActiveDoc(self):
        self._journal.record("SwApp", "ActiveDoc")
        return self._active


def new_fake_session(revision: str = "32.5.0"):
    """构建 (FakeSwApp, journal) 组合, 冒烟测试的统一入口。"""
    journal = ComJournal()
    return FakeSwApp(journal, revision), journal
