import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))
import infographic_text


def reading(
        *, text: tuple[str, ...] = ("FULL SWEEP", "FLOWY HEM", "LACE DETAIL"),
        panels: tuple[str, ...] = (
            "dominant upper skirt panel",
            "lower-left full outfit panel",
            "lower-right lace detail panel",
        ),
        instances: tuple[str, ...] = ("skirt", "full outfit", "lace detail"),
) -> infographic_text.InventoryReading:
    return infographic_text.InventoryReading(
        target_token="target-info",
        visible_text=text,
        panels=panels,
        garment_instances=instances,
    )


class InventoryCoordinationTest(unittest.TestCase):
    def test_two_same_image_readings_settle_exact_literal_and_instance_inventory(self) -> None:
        calls: list[str] = []

        def read(target_token: str) -> infographic_text.InventoryReading:
            calls.append(target_token)
            return reading()

        inventory = infographic_text.settle_inventory(
            "target-info", read=read, adjudicate=lambda *_args: self.fail(
                "matching readings must not be adjudicated"
            ),
        )

        self.assertEqual(calls, ["target-info", "target-info"])
        self.assertEqual(inventory.visible_text, (
            "FULL SWEEP", "FLOWY HEM", "LACE DETAIL",
        ))
        self.assertEqual(inventory.panels, (
            "dominant upper skirt panel",
            "lower-left full outfit panel",
            "lower-right lace detail panel",
        ))
        self.assertEqual(
            inventory.garment_instances,
            ("skirt", "full outfit", "lace detail"),
        )
        self.assertFalse(inventory.adjudicated)
        self.assertEqual(inventory.reading_count, 2)

    def test_disagreement_is_adjudicated_once_without_normalizing_flowy_hem(self) -> None:
        readings = iter((
            reading(),
            reading(text=("FULL SWEEP", "FLOWYHEM", "LACE DETAIL")),
        ))
        adjudications: list[tuple[str, object, object]] = []

        def adjudicate(
                target_token: str,
                first: infographic_text.InventoryReading,
                second: infographic_text.InventoryReading,
        ) -> infographic_text.InventoryReading:
            adjudications.append((target_token, first, second))
            return reading()

        inventory = infographic_text.settle_inventory(
            "target-info", read=lambda _token: next(readings),
            adjudicate=adjudicate,
        )

        self.assertEqual(len(adjudications), 1)
        self.assertEqual(inventory.visible_text[1], "FLOWY HEM")
        self.assertTrue(inventory.adjudicated)
        self.assertEqual(inventory.reading_count, 2)

    def test_paid_generation_starts_only_after_adjudicated_inventory_is_settled(self) -> None:
        events: list[str] = []
        readings = iter((
            reading(),
            reading(panels=("one combined panel",)),
        ))

        def read(_target_token: str) -> infographic_text.InventoryReading:
            events.append("read")
            return next(readings)

        def adjudicate(
                _target_token: str,
                _first: infographic_text.InventoryReading,
                _second: infographic_text.InventoryReading,
        ) -> infographic_text.InventoryReading:
            events.append("adjudicate")
            return reading()

        def paid_generate(
                inventory: infographic_text.InfographicInventory,
        ) -> str:
            events.append("paid-generate")
            self.assertTrue(inventory.settled)
            return "candidate.png"

        result = infographic_text.settle_then_generate(
            "target-info", read=read, adjudicate=adjudicate,
            paid_generate=paid_generate,
        )

        self.assertEqual(result, "candidate.png")
        self.assertEqual(events, ["read", "read", "adjudicate", "paid-generate"])

    def test_failed_adjudication_blocks_paid_generation(self) -> None:
        paid_calls: list[object] = []
        readings = iter((reading(), reading(text=("FLOWYHEM",))))

        with self.assertRaises(infographic_text.InventoryError):
            infographic_text.settle_then_generate(
                "target-info",
                read=lambda _token: next(readings),
                adjudicate=lambda *_args: reading(text=()),
                paid_generate=lambda inventory: paid_calls.append(inventory),
            )

        self.assertEqual(paid_calls, [])


if __name__ == "__main__":
    unittest.main()
