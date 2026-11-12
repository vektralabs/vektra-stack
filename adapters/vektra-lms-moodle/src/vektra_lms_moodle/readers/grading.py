"""Grading readers (plan group C): rubric definition, rubric fillings, marking-guide detection,
scale definition, and the (always unreadable) rubric display options.

- ``core_grading_get_definitions`` answers an area with no active method with the error
  ``invalid_method_name`` (HTTP 200 envelope): that is "grading method none", not a failure.
- A marking guide is only DETECTED (criteria count and total): its ``descriptionmarkers`` are
  teacher-only marker notes and are never copied into a domain value.
- Rubric criterion and level texts are readable, but whether a student may SEE them
  (``showdescriptionstudent`` and friends) is not exposed by any function on 4.3 or 5.2
  (spike): ``read_rubric_display_options`` is Unreadable by design and BR-011 excludes the
  descriptors.
- The WS exposes a scale only through the grade report (``rangeformatted`` "Poor-Excellent" and
  ``grademax`` = size): the first and last label and the size are readable, the full label list
  is not.
"""

from __future__ import annotations

import html
import re

from vektra_lms_moodle.client import MoodleWsClient, SiteRef
from vektra_lms_moodle.domain import (
    GradingView,
    MarkingGuideSummary,
    RubricCriterion,
    RubricCriterionFilling,
    RubricDefinitionView,
    RubricFilling,
    RubricLevel,
    ScaleDefinition,
)
from vektra_lms_moodle.readers._support import fetch, reader
from vektra_lms_moodle.result import Readable, Reading, Unreadable, UnreadableReason

NO_ACTIVE_METHOD = "invalid_method_name"


@reader
async def read_grading_view(
    client: MoodleWsClient, site: SiteRef, cmid: int
) -> Reading[GradingView]:
    """The active grading method of an assignment and its definition (rubric or guide)."""
    res = await fetch(
        client,
        site,
        "core_grading_get_definitions",
        {"cmids": [cmid], "areaname": "submissions", "activeonly": 1},
    )
    if isinstance(res, Unreadable):
        if res.detail_code == NO_ACTIVE_METHOD:
            return Readable(GradingView(cmid, "none", None, None))
        return res
    areas = [
        a for a in res.value.areas if a.cmid == cmid and a.areaname == "submissions"
    ]
    if len(areas) != 1:
        return Unreadable(UnreadableReason.PARTIAL, "grading_area_not_found")
    area = areas[0]
    method = area.activemethod or "none"
    defs = [d for d in area.definitions if d.method == method]
    if method == "none" or not defs:
        return Readable(GradingView(cmid, "none", None, None), res.evidence)
    d = defs[0]
    if method == "rubric" and d.rubric is not None:
        rubric = RubricDefinitionView(
            definitionid=d.id,
            name=d.name,
            criteria=tuple(
                RubricCriterion(
                    criterionid=c.id,
                    sortorder=c.sortorder,
                    description=c.description,
                    levels=tuple(
                        RubricLevel(lv.id, float(lv.score), lv.definition)
                        for lv in c.levels
                    ),
                )
                for c in sorted(d.rubric.rubric_criteria, key=lambda x: x.sortorder)
            ),
        )
        return Readable(GradingView(cmid, "rubric", rubric, None), res.evidence)
    if method == "guide" and d.guide is not None:
        guide = MarkingGuideSummary(
            definitionid=d.id,
            criteria_count=len(d.guide.guide_criteria),
            max_total=float(sum(c.maxscore for c in d.guide.guide_criteria)),
        )
        return Readable(GradingView(cmid, "guide", None, guide), res.evidence)
    return Readable(GradingView(cmid, method, None, None), res.evidence)


@reader
async def read_rubric_fillings(
    client: MoodleWsClient, site: SiteRef, definitionid: int
) -> Reading[tuple[RubricFilling, ...]]:
    """The level chosen per criterion and the remark, for every filled instance of a rubric
    (needs ``moodle/grade:managegradingforms``)."""
    res = await fetch(
        client,
        site,
        "core_grading_get_gradingform_instances",
        {"definitionid": definitionid, "since": 0},
    )
    if isinstance(res, Unreadable):
        return res
    rows = tuple(
        RubricFilling(
            instanceid=i.id,
            item_id=i.itemid,
            status=i.status,
            criteria=tuple(
                RubricCriterionFilling(c.criterionid, c.levelid, c.remark)
                for c in i.rubric.criteria
            ),
        )
        for i in res.value.instances
    )
    return Readable(rows, res.evidence)


async def read_rubric_display_options(
    client: MoodleWsClient, site: SiteRef, definitionid: int
) -> Reading[dict[str, bool]]:
    """``showdescriptionstudent`` and the other display options: NOT readable on 4.3 or 5.2.

    ``core_grading_get_definitions`` returns criteria and levels but no options, and no other
    function exposes them (spike, capability row ``rubric_display_settings``). Returning
    Unreadable WITHOUT calling the site is the whole contract: BR-011 then excludes the rubric
    descriptors from the model context, always.
    """
    del client, site, definitionid
    return Unreadable(UnreadableReason.PARTIAL, "rubric_display_unreadable")


_RANGE_SPLIT = re.compile(r"&ndash;|\u2013")


@reader
async def read_scale_definition(
    client: MoodleWsClient,
    site: SiteRef,
    courseid: int,
    userid: int,
    cmid: int,
    scale_id: int,
) -> Reading[ScaleDefinition]:
    """Size and end labels of a scale, from the grade report entry of any student."""
    res = await fetch(
        client,
        site,
        "gradereport_user_get_grade_items",
        {"courseid": courseid, "userid": userid},
    )
    if isinstance(res, Unreadable):
        return res
    items = [
        i
        for u in res.value.usergrades
        if u.userid == userid
        for i in u.gradeitems
        if i.cmid == cmid and i.itemtype == "mod"
    ]
    if len(items) != 1:
        return Unreadable(UnreadableReason.PARTIAL, "grade_item_not_found")
    item = items[0]
    ends = _RANGE_SPLIT.split(item.rangeformatted, maxsplit=1)
    if len(ends) != 2 or not ends[0] or not ends[1]:
        return Unreadable(UnreadableReason.UNKNOWN_SHAPE, "scale_range")
    return Readable(
        ScaleDefinition(
            scale_id=scale_id,
            size=int(item.grademax),
            first_label=html.unescape(ends[0]),
            last_label=html.unescape(ends[1]),
            labels=Unreadable(UnreadableReason.PARTIAL, "scale_items_not_exposed"),
        ),
        res.evidence,
    )
