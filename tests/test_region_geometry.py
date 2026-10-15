"""Exercise the real browser geometry helper in Node, without app data or jobs."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
if not NODE:
    bundled = Path('C:/Users/developer/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe')
    if bundled.is_file():
        NODE = str(bundled)


@unittest.skipUnless(NODE, 'Node is required for browser region geometry checks.')
class RegionGeometryTests(unittest.TestCase):
    def js(self, expression):
        source = (
            'require(process.argv[1]); const g=globalThis.NpkRegionGeometry; '
            'const result=(() => {' + expression + '})(); '
            'process.stdout.write(JSON.stringify(result));'
        )
        result = subprocess.run(
            [NODE, '-e', source, str(ROOT / 'static/region-editor.js')],
            capture_output=True, text=True, encoding='utf-8', timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_clamp_keeps_pixel_bounds_and_exact_time_without_mutating_input(self):
        result = self.js('''
          const original={x:-15.4,y:800,w:1500,h:900,start:1.25341,end:10.12567};
          const before=JSON.stringify(original);
          return {row:g.clamp(original,{width:1280,height:720}), unchanged:before===JSON.stringify(original)};
        ''')
        self.assertTrue(result['unchanged'])
        self.assertEqual(result['row'], dict(x=0, y=719, w=1280, h=1, start=1.25341, end=10.12567))

    def test_group_move_clamps_one_shared_delta_at_positive_edges(self):
        result = self.js('''
          const rows=[{x:10,y:20,w:80,h:30,start:0,end:4.12345},{x:200,y:60,w:60,h:50,start:2,end:6.23456}];
          const before=JSON.stringify(rows);
          return {rows:g.move(rows,500,500,{width:320,height:180}),unchanged:before===JSON.stringify(rows)};
        ''')
        self.assertTrue(result['unchanged'])
        self.assertEqual(result['rows'], [dict(x=70,y=90,w=80,h=30,start=0,end=4.12345),
                                          dict(x=260,y=130,w=60,h=50,start=2,end=6.23456)])

    def test_group_move_preserves_relative_positions_at_negative_edges(self):
        result = self.js('''
          return g.move([{x:10,y:20,w:80,h:30},{x:200,y:60,w:60,h:50}],-500,-500,{width:320,height:180});
        ''')
        self.assertEqual(result, [dict(x=0,y=0,w=80,h=30), dict(x=190,y=40,w=60,h=50)])

    def test_all_eight_resize_handles_keep_the_opposite_edges_fixed(self):
        result = self.js('''
          const row={x:100,y:100,w:200,h:100,start:0,end:12.123456};
          return Object.fromEntries(['nw','n','ne','e','se','s','sw','w'].map(handle=>
            [handle,g.resize(row,handle,20,15,{width:640,height:360})]));
        ''')
        expected = {
            'nw': (120,115,180,85), 'n': (100,115,200,85), 'ne': (100,115,220,85),
            'e': (100,100,220,100), 'se': (100,100,220,115), 's': (100,100,200,115),
            'sw': (120,100,180,115), 'w': (120,100,180,100),
        }
        for handle, bounds in expected.items():
            with self.subTest(handle=handle):
                self.assertEqual(tuple(result[handle][key] for key in ('x','y','w','h')), bounds)
                self.assertEqual(result[handle]['end'], 12.123456)

    def test_resize_cannot_cross_opposite_edge_or_escape_image(self):
        result = self.js('''
          const row={x:100,y:100,w:200,h:100,start:1.25,end:9.75}, result=[];
          for(const handle of ['nw','n','ne','e','se','s','sw','w'])
            for(const dx of [-10000,10000]) for(const dy of [-10000,10000])
              result.push(g.resize(row,handle,dx,dy,{width:640,height:360}));
          return result;
        ''')
        self.assertEqual(len(result), 32)
        for row in result:
            self.assertGreaterEqual(row['x'], 0)
            self.assertGreaterEqual(row['y'], 0)
            self.assertGreaterEqual(row['w'], 1)
            self.assertGreaterEqual(row['h'], 1)
            self.assertLessEqual(row['x'] + row['w'], 640)
            self.assertLessEqual(row['y'] + row['h'], 360)
            self.assertEqual((row['start'], row['end']), (1.25, 9.75))

    def test_aspect_resize_keeps_ratio_and_corner_anchor_at_frame_edges(self):
        result = self.js('''
          const row={x:100,y:100,w:200,h:100,start:0,end:9.4321};
          return Object.fromEntries(['nw','ne','se','sw'].map(handle=>{
            const dx=handle.includes('w')?-10000:10000, dy=handle.includes('n')?-10000:10000;
            return [handle,g.resize(row,handle,dx,dy,{width:640,height:360},true)];
          }));
        ''')
        anchors = {'nw':(300,200), 'ne':(100,200), 'se':(100,100), 'sw':(300,100)}
        for handle, row in result.items():
            with self.subTest(handle=handle):
                self.assertAlmostEqual(row['w']/row['h'], 2)
                self.assertAlmostEqual(row['x'] + (row['w'] if 'w' in handle else 0), anchors[handle][0])
                self.assertAlmostEqual(row['y'] + (row['h'] if 'n' in handle else 0), anchors[handle][1])
                self.assertGreaterEqual(row['x'], 0)
                self.assertGreaterEqual(row['y'], 0)
                self.assertLessEqual(row['x']+row['w'], 640)
                self.assertLessEqual(row['y']+row['h'], 360)
                self.assertEqual(row['end'], 9.4321)

    def test_union_includes_all_positions_and_disjoint_time_ranges(self):
        result = self.js('''
          return g.union([{x:20,y:80,w:40,h:50,start:1.234,end:2.345},
                          {x:5,y:30,w:10,h:20,start:6.789,end:7.891}]);
        ''')
        self.assertEqual(result, dict(x=5,y=30,w=55,h=100,start=1.234,end=7.891))

    def test_aspect_resize_shrinks_to_valid_minimum_instead_of_free_stretch(self):
        result = self.js('''
          const row={x:100,y:100,w:200,h:100,start:0,end:8};
          return Object.fromEntries(['nw','ne','se','sw'].map(handle=>
            [handle,g.resize(row,handle,handle.includes('w')?10000:-10000,0,
                            {width:640,height:360},true)]));
        ''')
        anchors = {'nw':(300,200), 'ne':(100,200), 'se':(100,100), 'sw':(300,100)}
        for handle, row in result.items():
            with self.subTest(handle=handle):
                self.assertGreaterEqual(row['w'], 1)
                self.assertGreaterEqual(row['h'], 1)
                self.assertAlmostEqual(row['w']/row['h'], 2)
                self.assertAlmostEqual(row['x']+(row['w'] if 'w' in handle else 0), anchors[handle][0])
                self.assertAlmostEqual(row['y']+(row['h'] if 'n' in handle else 0), anchors[handle][1])


if __name__ == '__main__':
    unittest.main()
