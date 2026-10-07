"""Regressions for replaced images and backgrounds used by the Bishop's homepage."""
import unittest

from PIL import Image

from browser import layout, network, paint
from browser.dom import Element


class ImageAndBackgroundTests(unittest.TestCase):
    def image_box(self, styles=None, size=(400, 200)):
        url = "https://example.test/photo.png"
        ctx = layout.LayoutContext(network.URL("https://example.test/"),
                                   {url: Image.new("RGB", size)}, 800, 600)
        node = Element("img", {"src": url})
        box = layout.ImageBox(node, styles or {}, ctx)
        box.compute_edges(800)
        return box

    def test_fixed_height_survives_max_width(self):
        box = self.image_box({"width": "400px", "height": "180px", "max-width": "100%"})
        self.assertEqual(box.compute_size(200), (200, 180))

    def test_auto_height_follows_constrained_width(self):
        box = self.image_box({"width": "400px", "max-width": "100%"})
        self.assertEqual(box.compute_size(200), (200, 100))

    def test_min_max_respect_border_box(self):
        box = self.image_box({"width": "300px", "height": "100px", "box-sizing": "border-box",
                              "max-width": "200px", "padding-left": "10px", "padding-right": "10px"})
        self.assertEqual(box.compute_size(800), (180, 100))

    def test_flex_width_does_not_rescale_explicit_height(self):
        box = self.image_box({"width": "400px", "height": "180px"})
        box.layout(0, 0, 800, layout.FloatContext(), forced_outer=200)
        self.assertEqual((box.width, box.height), (200, 180))

    def test_absolute_percentage_height_fills_image_frame(self):
        # Kadence's ratio images use this exact width/height/object-fit pattern.
        box = self.image_box({"position": "absolute", "width": "100%", "height": "100%",
                              "top": "0", "left": "0", "object-fit": "cover"})
        layout.layout_absolute(box, 10, 20, 200, 150, 0, 0)
        self.assertEqual((box.x, box.y, box.width, box.height), (10, 20, 200, 150))
        dl = paint.DisplayList()
        box.paint(dl)
        command = next(c for c in dl.commands if isinstance(c, paint.DrawImage))
        self.assertEqual(command.full_size, (300, 150))
        self.assertEqual(command.crop, (50, 0, 200, 150))

    def test_object_position_controls_cover_crop(self):
        box = self.image_box({"width": "100px", "height": "100px", "object-fit": "cover",
                              "object-position": "right top"})
        box.layout(0, 0, 800, layout.FloatContext())
        dl = paint.DisplayList()
        box.paint(dl)
        self.assertEqual(dl.commands[-1].crop, (100, 0, 100, 100))

    def test_scale_down_does_not_enlarge(self):
        box = self.image_box({"width": "600px", "height": "400px", "object-fit": "scale-down"})
        box.layout(0, 0, 800, layout.FloatContext())
        dl = paint.DisplayList()
        box.paint(dl)
        command = dl.commands[-1]
        self.assertEqual((command.left, command.top, command.full_size), (100, 100, (400, 200)))

    def test_automatic_dimensions_preserve_ratio_with_limits(self):
        box = self.image_box({"max-width": "200px", "max-height": "75px"})
        self.assertEqual(box.compute_size(800), (150, 75))
        box.style = {"min-width": "600px", "min-height": "400px"}
        self.assertEqual(box.compute_size(800), (800, 400))

    def test_homepage_background_sizes(self):
        box = self.image_box()
        self.assertEqual(layout._layer_size(box, "auto 40%", 400, 200, 1000, 500), (400, 200))
        self.assertEqual(layout._layer_size(box, "50% auto", 400, 200, 1000, 500), (500, 250))
        self.assertEqual(layout._layer_size(box, "cover", 400, 200, 200, 150), (300, 150))

    def test_background_calc_position_uses_remaining_space(self):
        self.assertEqual(layout._bg_position("calc(100% - 6px) 50%", 200, 40, 16), (194, 20))


if __name__ == "__main__":
    unittest.main()
