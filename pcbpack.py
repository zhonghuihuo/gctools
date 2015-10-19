#!/usr/bin/env python
#----------------------------------------------------------------------------
# 26-Jul-2015 ShaneG
#
# Added functionality for generating drill code from standalone Excellon files
# (with support for multiple hole diameters) and fixed alignment issues with
# the various layers.
#
# 21-Jul-2015 ShaneG
#
# Major refactoring and updates to make it part of the 'gctools' suite.
#
# 13-Nov-2014 ShaneG
#
# Some slight changes to the code for consistancy with the output generated
# by 'linegrinder'. The origin is now in the lower left corner, x increments
# upwards, y increments to the right. The boards are now treated with the
# same origin location (this is what linegrinder generates).
#
# Rotations are in the counter clockwise direction with the working origin
# at the bottom left corner which moves the source origin to the bottom
# right.
#
# 11-Nov-2014 ShaneG
#
# Tool to pack multiple PCB g-code files into a single panel.
#----------------------------------------------------------------------------
import re
from util import *
from string import Template
from random import randint
from os import listdir
from os.path import realpath, splitext, exists, join, basename
from optparse import OptionParser
from PIL import Image, ImageDraw

#--- Globals
CONFIG = None
CONTROL = {
  "pcbcut": -2.5,
  "safe": 3.0,
  }

#--- Template for OpenSCAM project
OPENSCAM_XML = """
<openscam>
  <!-- Note, all values are in mm regardless of 'units' option. -->
  <!-- NC Files -->
  <nc-files>
    ${filenames}
  </nc-files>

  <!-- Renderer -->
  <resolution v='0.355689'/>

  <!-- Workpiece -->
  <automatic-workpiece v='false'/>
  <workpiece-max v='(${panel_width},${panel_height},1)'/>
  <workpiece-min v='(0,0,0)'/>

  <tool_table>
    <tool length='10' number='1' radius='0.25' shape='CYLINDRICAL' units='MM'/>
  </tool_table>
</openscam>
"""

#----------------------------------------------------------------------------
# Helper functions
#----------------------------------------------------------------------------

def area(*args):
  """ Calculate the combined area of a number of elements

    Each element is expected to have a 'w' and 'h' attribute.
  """
  return sum([ float(a.w) * float(a.h) for a in args ])

def rotations(boards):
  """ Generator to walk through all rotation combinations
  """
  for iteration in range(2 ** len(boards)):
    candidate = list()
    flag = 1
    for index in range(len(boards)):
      board = boards[index].clone()
      if iteration & flag:
        board.rotated = True
      candidate.append(board)
    yield sorted(candidate, cmp = lambda x, y: cmp(x.h, y.h), reverse = True)

def findFile(path, filename):
  """ Find a file that ends with the given filename in the path

    Fails if more than one matching file exists or no files exist.
  """
  result = list()
  for f in listdir(path):
    if f.endswith(filename):
      result.append(f)
  if len(result) <> 1:
    return None
  return join(path, result[0])

def loadDrillFile(filename):
  """ Load an Excellon format drill file

    Returns a dictionary of lists mapping drill diameters to a list of X/Y
    positions (in mm). This is geared towards the files generated by
    DesignSpark.
  """
  global options
  header = True
  current = None
  tools = dict()
  results = dict()
  divisor = None
  scale = 25.4
  regex = re.compile("X([0-9]*)Y([0-9]*)")
  for line in open(filename, "r"):
    line = line.strip()
    if header:
      # Look for tool definitions (eg "T1C00.039")
      if line.startswith("T"):
        tools[line[1]] = round(scale * float(line[3:]), 1)
        # Figure out the divisor
        if divisor is None:
          divisor = 10.0 ** len(line[3:].split(".")[1])
        if options.merge:
          # Merge any bit <= 1.2mm into a single 1mm group
          if tools[line[1]] <= 1.2:
            tools[line[1]] = 1.0
      elif line.startswith("METRIC"):
        scale = 1.0
      elif line.startswith("%"):
        header = False
    else:
      if line.startswith("T"):
        # Tool selection
        current = tools[line[3]]
        if not results.has_key(current):
          results[current] = list()
      elif line.startswith("X"):
        # Position
        if current is not None:
          parts = regex.match(line).groups()
          if len(parts) == 2:
            x = scale * (int(parts[0], 10) / divisor)
            y = scale * (int(parts[1], 10) / divisor)
            results[current].append((x, y))
  return results

#----------------------------------------------------------------------------
# Manage board positioning
#
# The width and height of the board must include any spacing required and
# will be taken into account when modifying the gcode.
#----------------------------------------------------------------------------

class BoardPosition:
  """ Represents a board.

    Each board has a fixed size (width and height), a mutable location and
    a rotation flag.
  """

  def __init__(self, name, w, h):
    """ Constructor with dimensions
    """
    self.name = name
    self._width = w
    self._height = h
    self.reset()

  def __str__(self):
    return "Board %0.2f x %0.2f @ %0.2f, %0.2f (Rot = %s)" % (self.w, self.h, self.x, self.y, self.rotated)

  @property
  def w(self):
    if self.rotated:
      return self._height
    return self._width

  @w.setter
  def w(self, value):
    raise Exception("Cannot modify width after creation")

  @property
  def h(self):
    if self.rotated:
      return self._width
    return self._height

  @h.setter
  def h(self, value):
    raise Exception("Cannot modify height after creation")

  def reset(self):
    """ Restore to original (unrotated, untranslated) state
    """
    self.x = 0.0
    self.y = 0.0
    self.rotated = False

  def overlaps(self, other):
    """ Determine if this board overlaps another
    """
    return not (((self.x + self.w <= other.x) or
      ((other.x + other.w) <= self.x) or
      ((self.y + self.h) <= other.y) or
      ((other.y + other.h) <= self.y)))

  def contains(self, other):
    """ Determine if this board completely contains another
    """
    return ((self.x <= other.x) and
      ((self.x + self.w) >= (other.x + other.w)) and
      (self.y <= other.y) and
      ((self.y + self.h) >= (other.y + other.h)))

  def intersects(self, other):
    """ Determine if this board intesects another
    """
    return self.overlaps(other) or self.contains(other) or other.contains(self)

  def area(self):
    """ Return the area of the board
    """
    return self.width * self.height

  def clone(self):
    copy = BoardPosition(self.name, self.w, self.h)
    copy.x = self.x
    copy.y = self.y
    return copy

#----------------------------------------------------------------------------
# Manage PCBs
#
# A PCB is represented by a collection of gcode and excellon drill files.
#----------------------------------------------------------------------------

class PCB:
  """ Represent a single PCB
  """

  def __init__(self, name):
    """ Constructor

      Verify that all the files needed exist and load them
    """
    global CONFIG, CONTROL
    path = join(realpath(CONFIG['boards']), name)
    if not exists(path):
      raise Exception("No board directory found at '%s'" % path)
    # Set up basics
    self.name = name
    self.padding = 2.0 # TODO: Should be in config
    # Load the board outline
    filename = findFile(path, "Board Outline_EDGEMILL_GCODE.ngc")
    if filename is None:
      raise Exception("Missing board outline for '%s'" % name)
    self.outline = loadGCode(filename, BoxedLoader(start = GCommand("G04 P1"), end = GCommand("G00 X0 Y0"), inclusive = False))
    self.dx = -self.outline.minx
    self.dy = -self.outline.miny
    self.midpoint = self.outline.minx + ((self.outline.maxx - self.outline.minx) / 2)
    self.outline = self.outline.clone(Flip(xflip = self.midpoint), Translate(self.dx, self.dy))
    # Generate the drill gcode from the excellon data
    self.drills = dict()
    filename = findFile(path, "Drill Data - [Through Hole].drl")
    if filename is not None:
      drills = loadDrillFile(filename)
      # Generate the gcode
      for diam in drills.keys():
        gcd = GCode()
        gcd.append("G00 Z%0.4f" % CONTROL['safe'])
        for x, y in drills[diam]:
          gcd.append("G00 X%0.4f Y%0.4f" % (x, y))
          gcd.append("G01 Z%0.4f F%0.4f" % (CONTROL['pcbcut'], CONFIG['penetrate']))
          gcd.append("G00 Z%0.4f" % CONTROL['safe'])
        # Adjust to match the rest of the files
        self.drills[diam] = gcd.clone(Flip(xflip = self.midpoint), Translate(self.dx, self.dy))
    # Load the top copper (if present)
    self.top = None
    filename = findFile(path, "Top Copper_ISOLATION_GCODE.ngc")
    if filename is not None:
      self.top = loadGCode(filename, BoxedLoader(start = GCommand("G04 P1"), end = GCommand("G00 X0 Y0"), inclusive = False))
      self.top = self.top.clone(Translate(self.dx, self.dy))
    # Load the bottom copper
    filename = findFile(path, "Bottom Copper_ISOLATION_GCODE.ngc")
    if filename is None:
      raise Exception("Missing bottom copper for '%s'" % name)
    self.bottom = loadGCode(filename, BoxedLoader(start = GCommand("G04 P1"), end = GCommand("G00 X0 Y0"), inclusive = False))
    # Add outlines for the drill holes (avoid tearing)
    global options
    if options.pads:
      if drills is not None:
        for diam in drills.keys():
          if diam >= 1.0: # Holes < 1.0 mm don't need the outline
            for x, y in drills[diam]:
              self.bottom.circle(x, y, (diam - CONFIG['toolwidth']) / 2, self.bottom.minz, self.bottom.maxz, step = 0.254)
    self.bottom = self.bottom.clone(Flip(xflip = self.midpoint), Translate(self.dx, self.dy))
    # Generate an outline as well (to avoid tearing)
    delta = abs(max(self.dx, self.dy)) / 2
    x1, y1 = self.outline.minx + delta, self.outline.miny + delta
    x2, y2 = self.outline.maxx - delta, self.outline.maxy - delta
    safe, cut = self.bottom.maxz, self.bottom.minz
    feed, insert = 250.0, 500.0 # TODO: Should be extracted from file
    outline = GCode()
    outline.append("G0 Z%0.4f" % safe)
    outline.append("G0 X%0.4f Y%0.4f" % (x1, y1))
    outline.append("G1 Z%0.4f F%0.4f" % (cut, insert))
    for point in ((x2, y1), (x2, y2), (x1, y2), (x1, y1)):
      outline.append("G1 X%0.4f Y%0.4f F%0.4f" % (point[0], point[1], feed))
    outline.append("G0 Z%0.4f" % safe)
    # Merge it with the bottom copper
    outline.append(self.bottom)
    self.bottom = outline

  def _rotate(self, gcode):
    """ Apply the rotation and translation needed for board layout
    """
    return gcode.clone(Rotate(-90.0), Translate(0.0, self.outline.maxx))

  def getBoard(self):
    """ Return a BoardPosition for this PCB
    """
    return BoardPosition(self.name, self.outline.maxx + (2 * self.padding), self.outline.maxy + (2 * self.padding))

  def generateTopCopper(self, gcode, position, panel_height):
    # TODO: This is harder than it looks :(
    pass

  def generateBottomCopper(self, gcode, position):
    if self.bottom is None:
      return
    bottom = self.bottom
    # Rotate if needed
    if position.rotated:
      bottom = self._rotate(bottom)
    # Translate to the right spot
    bottom = bottom.clone(Translate(self.padding + position.x, self.padding + position.y))
    # Add to the full gcode
    gcode.append("(INFO: %s @ %04.f, %0.4f rot = %s)" % (self.name, position.x, position.y, position.rotated))
    gcode.append(bottom)

  def generateOutline(self, gcode, position):
    """ Generate the board outline gcode given the position
    """
    outline = self.outline
    # Rotate if needed
    if position.rotated:
      outline = self._rotate(outline)
    # Translate to the right spot
    outline = outline.clone(Translate(self.padding + position.x, self.padding + position.y))
    # Add to the full gcode
    gcode.append("(INFO: %s @ %04.f, %0.4f rot = %s)" % (self.name, position.x, position.y, position.rotated))
    gcode.append(outline)

  def generateDrills(self, drills, position):
    """ Generate the drill files for various diameters
    """
    for diam in self.drills.keys():
      drill = self.drills[diam]
      # Rotate if needed
      if position.rotated:
        drill = self._rotate(drill)
      # Translate to the right spot
      drill = drill.clone(Translate(self.padding + position.x, self.padding + position.y))
      # Add to the full gcode
      if not drills.has_key(diam):
        drills[diam] = GCode()
      drills[diam].append("(INFO: %s @ %04.f, %0.4f rot = %s)" % (self.name, position.x, position.y, position.rotated))
      drills[diam].append(drill)

#----------------------------------------------------------------------------
# Manage panel layout
#
# A panel has a fixed size and (optionally) a set of fixed zones that cannot
# be used (mounting screws for example).
#----------------------------------------------------------------------------

class Panel:
  """ Represents a single panel
  """

  def __init__(self, name):
    """ Create the panel from the named configuration
    """
    global CONFIG
    # Set up state
    self.w = CONFIG['panels'][name]['width']
    self.h = CONFIG['panels'][name]['height']
    self.padding = CONFIG['panels'][name].get('padding', 2)
    self.description = CONFIG['panels'][name].get('description', "(undefined)")
    self.locked = list()
    if CONFIG['panels'][name].has_key("locked"):
      for lockInfo in CONFIG['panels'][name]['locked']:
        lock = BoardPosition("_lock_", lockInfo['w'], lockInfo['h'])
        lock.x = lockInfo['x']
        lock.y = lockInfo['y']
        self.locked.append(lock)

  def area(self):
    return area(self) - area(*self.locked)

  def consumed(self, boards):
    """ Determine the consumed area
    """
    w, h = 0, 0
    for b in boards:
      if b.name != "_lock_":
        w = max(w, b.x + b.w)
        h = max(h, b.y + b.h)
    return w * h

  def willFit(self, board):
    """ Determine if the board will fit
    """
    if (board.w > (self.w - (2 * self.padding))) and (board.w > (self.h - (2 * self.padding))):
      return False
    if (board.h > (self.w - (2 * self.padding))) and (board.h > (self.h - (2 * self.padding))):
      return False
    return True

  def findPosition(self, layout, board):
    LOG.DEBUG("Positioning %s" % board)
    for x in range(0, int(self.w - board.w - 1)):
      for y in range(0, int(self.h - board.h - 1)):
        LOG.DEBUG("  Testing %d, %d" % (x, y))
        board.x = x
        board.y = y
        # Does it overlap ?
        safe = True
        for existing in layout:
          LOG.DEBUG("Checking against %s" % existing)
          if board.intersects(existing):
            safe = False
        if safe:
          LOG.DEBUG("  Position is safe")
          return True
    return False

  def layout(self, *boards):
    """ Layout the set of boards on the panel
    """
    best = None
    for candidate in rotations(boards):
      # This is an ugly brute force approach
      current = list(self.locked)
      placed = True
      for board in candidate:
        if self.findPosition(current, board):
          LOG.DEBUG("Placed %s" % board)
          current.append(board)
        else:
          placed = False
          break
      # Did we place all the boards ?
      if placed:
        # Update the 'best' solution
        if (best is None) or (self.consumed(current) < self.consumed(best)):
          best = current
    self.layout = best
    return self.layout is not None

  #--------------------------------------------------------------------------
  # Utility methods
  #--------------------------------------------------------------------------

  def createImage(self, filename):
    """ Generate an image for the current layout.
    """
    img = Image.new("RGB", (int(self.w * 4), int(self.h * 4)), "white")
    drw = ImageDraw.Draw(img)
    for board in self.layout:
      color = "green"
      if board.name == "_lock_":
        color = "red"
      rect = (int(board.x * 4), int(board.y * 4), int((board.x + board.w) * 4), int((board.y + board.h) * 4))
      drw.rectangle(rect, fill = "black")
      rect = (rect[0] + 1, rect[1] + 1, rect[2] - 1, rect[3] - 1)
      drw.rectangle(rect, fill = color)
    img = img.transpose(Image.FLIP_TOP_BOTTOM)
    img.save(filename)

  def __str__(self):
    return "%s - %0.1f x %0.1f mm" % (self.description, self.w, self.h)

#----------------------------------------------------------------------------
# Helpers
#----------------------------------------------------------------------------

BOARD_CACHE = dict()

def loadBoard(name):
  """ Load the named board from the repository
  """
  # TODO: Implement this. For now we generate a random board if we haven't
  #       seen the name before
  global BOARD_CACHE
  board = BOARD_CACHE.get(name, None)
  if board is None:
    # Create a new one and cache it
    board = BoardPosition(
      name,
      15 + randint(0, 45),
      15 + randint(0, 45)
      )
    LOG.DEBUG(str(board))
    BOARD_CACHE[name] = board
  # Done
  return board

#----------------------------------------------------------------------------
# Main program
#----------------------------------------------------------------------------

if __name__ == "__main__":
  # Load the configuration
  cfg = splitext(realpath(__file__))[0] + ".json"
  if not exists(cfg):
    LOG.FATAL("Could not find configuration file '%s'" % cfg)
  try:
    CONFIG = fromJSONFile(cfg)
  except Exception, ex:
    LOG.FATAL("Could not load configuration file '%s' - %s" % (cfg, ex))
  # Process command line arguments
  parser = OptionParser()
  parser.add_option("-d", "--debug", action="store_true", default=False, dest="debug")
  parser.add_option("-n", "--no-optimise", action="store_false", default=True, dest="optimise")
  parser.add_option("-o", "--output", action="store", type="string", dest="output")
  parser.add_option("-p", "--panel", action="store", type="string", dest="panel")
  parser.add_option("-c", "--cut", action="store", type="float", dest="pcbcut")
  parser.add_option("-s", "--no-pads", action="store_false", default=True, dest="pads")
  parser.add_option("-m", "--merge", action="store_true", default=False, dest="merge")
  parser.add_option("-f", "--feed", action="store", type="float", dest="feedrate")
  options, args = parser.parse_args()
  # Check for required options
  for required in ("output", "panel"):
    if getattr(options, required) is None:
      LOG.FATAL("Missing required option '%s'" % required)
  if options.debug:
    LOG.severity = Logger.MSG_DEBUG
  else:
    LOG.severity = Logger.MSG_INFO
  # Set up the panel
  try:
    panel = Panel(options.panel)
  except Exception, ex:
    LOG.FATAL("Could not load panel definition '%s'" % options.panel)
  LOG.DEBUG("Panel - %s" % panel)
  # Load boards
  pcbs = dict()
  boards = list()
  count = 1
  for name in args:
    havePCB = True
    if not pcbs.has_key(name):
      try:
        pcbs[name] = PCB(name)
      except Exception, ex:
        # See if it is an integer count
        try:
          count = int(name)
          havePCB = False
        except:
          LOG.FATAL(str(ex))
    if havePCB:
      board = pcbs[name].getBoard()
      if not panel.willFit(board):
        LOG.FATAL("Board %s will not fit on this panel" % name)
      for i in range(count):
        boards.append(board)
      count = 1
  if len(boards) == 0:
    LOG.FATAL("No boards specified on command line")
  # Make sure they can reasonably fit
  if area(*boards) > panel.area():
    LOG.FATAL("This board combination cannot fit on the selected panel - board area = %0.2f, panel area = %0.2f" % (area(*boards), panel.area()))
  # Do the layout
  if not panel.layout(*boards):
    LOG.FATAL("Unable to find a combination that will fit")
  # Show the current layout
  panel.createImage("%s.png" % options.output)
  LOG.INFO("Selected layout ...")
  for board in panel.layout:
    if board.name <> "_lock_":
      LOG.INFO("  %s" % board)
  # Now we generate the output files
  top = GCode()
  bottom = GCode()
  outline = GCode()
  drills = dict()
  for board in panel.layout:
    if board.name <> "_lock_":
      pcbs[board.name].generateTopCopper(top, board, panel.h)
      pcbs[board.name].generateBottomCopper(bottom, board)
      pcbs[board.name].generateOutline(outline, board)
      pcbs[board.name].generateDrills(drills, board)
  # Generate optimised copies if requested
  if options.optimise:
    LOG.INFO("Optimising ...")
    LOG.INFO("  Top copper")
    top = optimise(top)
    LOG.INFO("  Bottom copper")
    bottom = optimise(bottom)
    LOG.INFO("  Board outline")
    outline = optimise(outline)
    for diam in drills.keys():
      LOG.INFO("  Drill (%0.1fmm)" % diam)
      drills[diam] = optimise(drills[diam])
  # Adjust the feed rate if required
  feedrate = getattr(options, "feedrate")
  if feedrate is not None:
    flt = FeedRate(feedrate)
    top = top.clone(flt)
    bottom = bottom.clone(flt)
  # Save all the main files
  filenames = list()
  settings = getSettings(CONTROL, options)
  for filename, gcode in (("_01_top.ngc", top), ("_02_bottom.ngc", bottom), ("_99_outline.ngc", outline.clone(ZLevel(cut = settings['pcbcut'])))):
    if gcode.minx is not None:
      # Correct arcs and adjust safe height
      gcode = gcode.clone(CorrectArc(), ZLevel(safe = settings['safe']))
      # Write the file
      filename = options.output + filename
      filenames.append(filename)
      LOG.INFO("Generating %s" % filename)
      saveGCode(filename, gcode, prefix = settings['prefix'], suffix = settings['suffix'])
      LOG.INFO("  %s" % str(gcode))
      gcode.render(splitext(filename)[0] + ".png")
  # Save the drill files
  index = 3
  for diam in sorted(drills.keys()):
    # Correct arcs and adjust safe/cutting depths
    drills[diam] = drills[diam].clone(CorrectArc(), ZLevel(safe = settings['safe'], cut = settings['pcbcut']))
    # Write the file
    filename = "%s_%02d_drill_%0.1f.ngc" % (options.output, index, float(diam))
    filenames.append(filename)
    LOG.INFO("Generating %s" % filename)
    saveGCode(filename, drills[diam], prefix = settings['prefix'], suffix = settings['suffix'])
    LOG.INFO("  %s" % str(drills[diam]))
    drills[diam].render(splitext(filename)[0] + ".png")
    index = index + 1
  # Finally generate a OpenSCAM project with all the files
  with open("%s.xml" % options.output, "w") as output:
    output.write(Template(OPENSCAM_XML).safe_substitute({
      "panel_width": str(panel.w),
      "panel_height": str(panel.h),
      "filenames": " ".join([ basename(f) for f in filenames ])
      }))

