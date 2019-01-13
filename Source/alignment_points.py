# -*- coding: utf-8; -*-
"""
Copyright (c) 2018 Rolf Hempel, rolf6419@gmx.de

This file is part of the PlanetarySystemStacker tool (PSS).
https://github.com/Rolf-Hempel/PlanetarySystemStacker

PSS is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with PSS.  If not, see <http://www.gnu.org/licenses/>.

"""

import glob
from math import ceil
from time import time

import matplotlib.pyplot as plt
from numpy import arange, amax, stack, amin, float32, uint8, zeros
from scipy import ndimage
from skimage.feature import register_translation

from align_frames import AlignFrames
from configuration import Configuration
from exceptions import NotSupportedError
from frames import Frames
from miscellaneous import Miscellaneous
from rank_frames import RankFrames


class AlignmentPoints(object):
    """
        Create a rectangular grid of potential places for alignment points. For each location
        create a small "alignment box" around it. For each alignment box test if there is enough
        structure and brightness in the picture to use it as an alignment point. For all
        alignment points in all frames, compute the local shifts relative to the mean frame.

    """

    def __init__(self, configuration, frames, rank_frames, align_frames):
        """
        Initialize the AlignmentPoints object and compute the mean frame.

        :param configuration: Configuration object with parameters
        :param frames: Frames object with all video frames
        :param rank_frames: RankFrames object with global quality ranks (between 0. and 1.,
                            1. being optimal) for all frames
        :param align_frames: AlignFrames object with global shift information for all frames
        """

        self.configuration = configuration
        self.frames = frames
        self.rank_frames = rank_frames
        self.align_frames = align_frames
        self.alignment_points = None
        self.alignment_points_dropped_dim = None
        self.alignment_points_dropped_structure = None

        self.stack_size = None

        # Reset the list of alignment points registered with each frame.
        self.frames.reset_alignment_point_lists()

    def ap_locations(self, num_pixels, half_box_width, search_width, step_size, even):
        """
        Compute optimal alignment patch coordinates in one coordinate direction. Place boundary
        neighbors as close as possible to the boundary.

        :param num_pixels: Number of pixels in the given coordinate direction
        :param half_box_width: Half-width of the alignment boxes
        :param search_width: Maximum search width for alignment point matching
        :param step_size: Distance of alignment boxes
        :param even: If True, compute locations for even row indices. Otherwise, for odd ones.
        :return: List of alignment point coordinates in the given direction
        """

        # The number of interior alignment boxes in general is not an integer. Round to the next
        # higher number.
        num_interior_odd = int(
            ceil(float(num_pixels - 2* (half_box_width + search_width)) / float(step_size)))
        # Because alignment points are arranged in a staggered grid, in even rows there is one point
        # more.
        num_interior_even = num_interior_odd + 1

        # The precise distance between alignment points will differ slightly from the specified
        # step_size. Compute the exact distance. Integer locations will be rounded later.
        distance_corrected = float(num_pixels - 2 * half_box_width - 2 * search_width) / float(
            num_interior_odd)

        # Compute the AP locations, separately for even and odd rows.
        if even:
            locations = []
            for i in range(num_interior_even):
                locations.append(int(search_width + half_box_width + i * distance_corrected))
        else:
            locations = []
            for i in range(num_interior_odd):
                locations.append(int(
                    search_width + half_box_width + 0.5 * distance_corrected +
                    i * distance_corrected))
        return locations

    def create_ap_grid(self, mean_frame):
        """
        Create a 2D staggered grid of alignment points. For each AP compute its center coordinates,
        and the coordinate limits of its alignment box and alignment patch. Only alignment points
        which satisfy the conditions on brightness, contrast and structure are eventually added to
        the list.

        :param mean_frame: Mean frame as computed by "align_frames.average_frame".

        :return: List of alignment points.
        """

        # Size of the mean frame in y and x directions
        num_pixels_y = mean_frame.shape[0]
        num_pixels_x = mean_frame.shape[1]
        # The alignment box is the object on which the displacement computation is performed.
        half_box_width = self.configuration.alignment_points_half_box_width
        # The alignment patch is the area which is stacked after a rigid displacement.
        half_patch_width = self.configuration.alignment_points_half_patch_width
        # Maximum displacement searched for in the alignment process.
        search_width = self.configuration.alignment_points_search_width
        # Number of pixels in one coordinate direction between alignment points
        step_size = self.configuration.alignment_points_step_size
        # Minimum structure value for an alignment point (between 0. and 1.)
        structure_threshold = self.configuration.alignment_points_structure_threshold
        # The brightest pixel must be brighter than this value (0 < value <256)
        brightness_threshold = self.configuration.alignment_points_brightness_threshold
        # The difference between the brightest and darkest pixel values must be larger than this
        # value (0 < value < 256)
        contrast_threshold = self.configuration.alignment_points_contrast_threshold

        # Compute y and x coordinate locations of alignemnt points. Note that the grid is staggered.
        ap_locations_y = self.ap_locations(num_pixels_y, half_box_width, search_width, step_size,
                                           True)
        ap_locations_x_even = self.ap_locations(num_pixels_x, half_box_width, search_width,
                                                step_size, True)
        ap_locations_x_odd = self.ap_locations(num_pixels_x, half_box_width, search_width,
                                               step_size, False)
        ap_locations_x_odd_len_minus_1 = len(ap_locations_x_odd) - 1

        # Standard alignment points are those which are created initially with this method.
        self.num_standard_aps = 0
        self.alignment_points = []
        self.alignment_points_dropped_dim = []
        self.alignment_points_dropped_structure = []

        # Create alignment point rows, start with an even one.
        even = True
        for y in ap_locations_y:
            # Create x coordinate, depending on the y row being even or odd (staggered grid).
            if even:
                ap_locations_x = ap_locations_x_even
            else:
                ap_locations_x = ap_locations_x_odd

            # For each location create an alignment point.
            for index_x, x in enumerate(ap_locations_x):
                # For odd rows: Fill the space left of the first patch and right of the last patch.
                if not even and index_x == 0:
                    alignment_point = self.new_alignment_point(mean_frame, self.frames.color, y, x,
                                                               half_box_width,
                                                               half_patch_width, num_pixels_y,
                                                               num_pixels_x, search_width,
                                                               extend_x_low=True)
                elif not even and index_x == ap_locations_x_odd_len_minus_1:
                    alignment_point = self.new_alignment_point(mean_frame, self.frames.color, y, x,
                                                               half_box_width,
                                                               half_patch_width, num_pixels_y,
                                                               num_pixels_x, search_width,
                                                               extend_x_high=True)
                else:
                    alignment_point = self.new_alignment_point(mean_frame, self.frames.color, y, x,
                                                               half_box_width,
                                                               half_patch_width, num_pixels_y,
                                                               num_pixels_x, search_width)

                # Increase the counter of standard APs.
                self.num_standard_aps += 1
                # Compute structure and brightness information for the alignment box.
                max_brightness = amax(alignment_point['reference_box'])
                min_brightness = amin(alignment_point['reference_box'])
                # If the alignment box satisfies the brightness conditions, add the AP to the list.
                if max_brightness > brightness_threshold and max_brightness - \
                        min_brightness > contrast_threshold:

                    # Check if the fraction of dark pixels exceeds a threshold.
                    box = alignment_point['reference_box']
                    fraction = (box < brightness_threshold).sum() / float(box.shape[0]*box.shape[1])
                    if fraction > self.configuration.alignment_points_dim_fraction_threshold:

                        # Compute the center of mass of the brightness distribution within the box,
                        # and shift the box center to this location.
                        com = ndimage.measurements.center_of_mass(box)
                        shift_y = int(com[0])
                        shift_x = int(com[1])
                        y_adapted = y + shift_y
                        x_adapted = x + shift_x
                        box_increment = max(shift_y, shift_x)

                        # Increase the box and patch sizes to avoid holes in the stacking image.
                        half_box_width_adapted = half_box_width + box_increment
                        half_patch_width_adapted = half_patch_width + box_increment

                        # Replace the alignment point with a new one, using the updated
                        # coordinates and box / patch sizes.
                        alignment_point = self.new_alignment_point(mean_frame,
                            self.frames.color, y_adapted, x_adapted, half_box_width_adapted,
                            half_patch_width_adapted, num_pixels_y, num_pixels_x, search_width,
                            extend_x_low=not even and index_x == 0,
                            extend_x_high=not even and index_x == ap_locations_x_odd_len_minus_1)

                    alignment_point['structure'] = Miscellaneous.quality_measure(
                        alignment_point['reference_box'])
                    self.alignment_points.append(alignment_point)
                else:
                    # If a point does not satisfy the conditions, add it to the dropped list.
                    self.alignment_points_dropped_dim.append(alignment_point)

            # Switch between even and odd rows.
            even = not even

        # Normalize the structure information for all alignment point boxes by dividing by the
        # maximum value.
        structure_max = max(
            alignment_point['structure'] for alignment_point in self.alignment_points)
        alignment_points_dropped_structure_indices = []
        for alignment_point_index, alignment_point in enumerate(self.alignment_points):
            alignment_point['structure'] /= structure_max
            # Remove alignment points with too little structure.
            if alignment_point['structure'] < structure_threshold:
                alignment_points_dropped_structure_indices.append(alignment_point_index)
                self.alignment_points_dropped_structure.append(alignment_point)

        # Remove alignment points which do not satisfy the structure condition, if there is any.
        if alignment_points_dropped_structure_indices:
            alignment_points_new = []
            dropped_index = 0
            for alignment_point_index, alignment_point in enumerate(self.alignment_points):
                if alignment_point_index != alignment_points_dropped_structure_indices[dropped_index]:
                    alignment_points_new.append(alignment_point)
                elif dropped_index < len(alignment_points_dropped_structure_indices)-1:
                        dropped_index += 1
            self.alignment_points = alignment_points_new

    @staticmethod
    def new_alignment_point(mean_frame, color, y, x, half_box_width, half_patch_width, num_pixels_y,
                            num_pixels_x, search_width, extend_x_low=False, extend_x_high=False):
        """
        Create a new alignment point. This method is called in creating the initial alignment point
        grid. Later it can be invoked by the user to add single alignment points.

        :param mean_frame: Mean frame as computed by "align_frames.average_frame"
        :param color: True, if frames are RGB color. False otherwise.
        :param y: y coordinate of alignment point center
        :param x: x coordinate of alignment point center
        :param half_box_width: Half-width of the alignment boxes
        :param half_patch_width: Half-width of the alignment patch (used in stacking)
        :param num_pixels_y: Number of pixels in y direction
        :param num_pixels_x: Number of pixels in x direction
        :param search_width: Maximum search width for alignment point matching
        :param extend_x_low: True, if patch is to be extended to the left frame boundary.
                             False otherwise.
        :param extend_x_high: True, if patch is to be extended to the right frame boundary.
                              False otherwise.
        :return:
        """

        alignment_point = {}
        alignment_point['y'] = y
        alignment_point['x'] = x
        alignment_point['box_y_low'] = max(0, y - half_box_width)
        alignment_point['box_y_high'] = min(num_pixels_y - search_width, y + half_box_width)
        alignment_point['box_x_low'] = max(0, x - half_box_width)
        alignment_point['box_x_high'] = min(num_pixels_x - search_width, x + half_box_width)
        alignment_point['patch_y_low'] = max(0, y - half_patch_width)
        alignment_point['patch_y_high'] = min(num_pixels_y, y + half_patch_width)
        if extend_x_low:
            alignment_point['patch_x_low'] = 0
            alignment_point['patch_x_high'] = min(num_pixels_x, x + half_patch_width)
        elif extend_x_high:
            alignment_point['patch_x_low'] = max(0, x - half_patch_width)
            alignment_point['patch_x_high'] = num_pixels_x
        else:
            alignment_point['patch_x_low'] = max(0, x - half_patch_width)
            alignment_point['patch_x_high'] = min(num_pixels_x, x + half_patch_width)
        # Initialize lists with neighboring aps with low structure or low light.
        alignment_point['low_structure_neighbors'] = None
        alignment_point['dim_neighbors'] = None
        # Allocate space for the stacking buffer.
        if color:
            alignment_point['stacking_buffer'] = zeros(
                [alignment_point['patch_y_high'] - alignment_point['patch_y_low'],
                 alignment_point['patch_x_high'] - alignment_point['patch_x_low'], 3],
                dtype=float32)
        else:
            alignment_point['stacking_buffer'] = zeros(
                [alignment_point['patch_y_high'] - alignment_point['patch_y_low'],
                 alignment_point['patch_x_high'] - alignment_point['patch_x_low']],
                dtype=float32)

        # Cut out the reference box from the mean frame, used in alignment.
        box = mean_frame[alignment_point['box_y_low']:alignment_point['box_y_high'],
              alignment_point['box_x_low']:alignment_point['box_x_high']]
        alignment_point['reference_box'] = box

        return alignment_point

    def remove_alignment_point(self, alignment_point):
        """
        Remove an alignment point from the current list. If it is a "standard" point allocated
        by "create_ap_grid", the AP is transferred to the list of failed APs (too dim). This way,
        the area covered by the AP will be filled during stacking, using the shift at some
        neighboring point. If the AP to be removed was allocated by the user, it is just removed
        from the list.

        :param alignment_point: Alignment point object to be removed
        :return: True, if the AP was removed successfully. False, otherwise.
        """

        # The index calculation can fail if the AP is not in the list.
        try:
            ap_index = self.alignment_points.index(alignment_point)
        except:
            return False

        # Treat user-allocated APs separately. Just remove it from the list.
        if ap_index >= self.num_standard_aps:
            self.alignment_points = self.alignment_points[0:ap_index] +\
                                   self.alignment_points[ap_index+1:]
        else:
            # For a standard AP, transfer it to the list of dropped APs and decrement the counter
            # of standard APs.
            self.alignment_points_dropped_dim.append(self.alignment_points.pop(ap_index))
            self.num_standard_aps -= 1

        return True

    def find_alignment_points(self, y_low, y_high, x_low, x_high):
        """
        Find all alignment points the centers of which are within given (y, x) bounds.

        :param y_low: Lower y pixel coordinate bound
        :param y_high: Upper y pixel coordinate bound
        :param x_low: Lower x pixel coordinate bound
        :param x_high: Upper x pixel coordinate bound
        :return: List of all alignment points with centers within the given coordinate bounds.
                 If no AP satisfies the condition, return an empty list.
        """

        ap_list = []
        for ap in self.alignment_points:
            if y_low <= ap['y'] <= y_high and x_low <= ap['x'] <= x_high:
                ap_list.append(ap)
        return ap_list

    def find_alignment_point_neighbors(self):
        """
        Go through the lists of alignment points which during "create_ap_grid" did not satisfy
        either the brightness/contrast or the structure conditions. For each such point find the
        closest "real" alignment point. Put the "failed" alignment point on the neighbor list of
        its "real" neighbor.

        In stacking, for the "failed" APs frame ranks and shifts will be copied from their "real"
        neighbor.

        :return: -
        """
        # Initialize the neighbor lists of all "real" alignment points.
        for ap in self.alignment_points:
            ap['low_structure_neighbors'] = []
            ap['dim_neighbors'] = []

        # There are two lists to process: First those APs which have too little structure.
        for ap_low_structure in self.alignment_points_dropped_structure:
            self.find_neighbor(ap_low_structure['y'], ap_low_structure['x'], self.alignment_points)[
                'low_structure_neighbors'].append(ap_low_structure)

        # And now the same for the points where the brightness is too dim or the contrast too low.
        for ap_dim in self.alignment_points_dropped_dim:
            self.find_neighbor(ap_dim['y'], ap_dim['x'], self.alignment_points)[
                'dim_neighbors'].append(ap_dim)
        pass

    @staticmethod
    def find_neighbor(ap_y, ap_x, alignment_points):
        """
        For a given (y, x) position find the closest "real" alignment point.

        :param ap_y: y cocrdinate of location of interest
        :param ap_x: x cocrdinate of location of interest
        :param alignment_points: List of alignment points to be searched

        :return: Alignment point object of closest AP
        """
        min_distance_squared = 1.e30
        ap_neighbor = None
        for ap in alignment_points:
            distance_squared = (ap['y'] - ap_y) ** 2 + (ap['x'] - ap_x) ** 2
            if distance_squared < min_distance_squared:
                ap_neighbor = ap
                min_distance_squared = distance_squared
        return ap_neighbor

    def compute_frame_qualities(self):
        """
        For each alignment point compute a ranking of best frames. Store the list in the
        alignment point dictionary with the key 'best_frame_indices'.

        Consider the special case that sampled-down Laplacians have been stored for frame ranking.
        In this case they can be re-used for ranking the boxes around alignment points (but only
        if "Laplace" has been selected for alignment point ranking).

        :return: -
        """

        # Compute the stack size from the given percentage. Take at least one frame.
        self.stack_size = max(int(ceil(
                self.frames.number * self.configuration.alignment_points_frame_percent / 100.)), 1)
        # Select the ranking method.
        if self.configuration.alignment_points_rank_method == "xy gradient":
            method = Miscellaneous.local_contrast
        elif self.configuration.alignment_points_rank_method == "Laplace":
            method = Miscellaneous.local_contrast_laplace
        elif self.configuration.alignment_points_rank_method == "Sobel":
            method = Miscellaneous.local_contrast_sobel
        else:
            raise NotSupportedError(
                "Ranking method " + self.configuration.alignment_points_rank_method +
                " not supported")

        if self.configuration.rank_frames_method != "Laplace" or \
                self.configuration.alignment_points_rank_method != "Laplace":
            # There are no stored Laplacians, or they cannot be used for the specified method.
            # Cycle through all alignment points:
            for alignment_point in self.alignment_points:
                alignment_point['frame_qualities'] = []
                # Cycle through all frames. Use the blurred monochrome image for ranking.
                for frame_index, frame in enumerate(self.frames.frames_mono_blurred):
                    # Compute patch bounds within the current frame.
                    y_low = max(0, alignment_point['patch_y_low'] + self.align_frames.dy[frame_index])
                    y_high = min(self.frames.shape[0],
                                 alignment_point['patch_y_high'] + self.align_frames.dy[frame_index])
                    x_low = max(0, alignment_point['patch_x_low'] + self.align_frames.dx[frame_index])
                    x_high = min(self.frames.shape[1],
                                 alignment_point['patch_x_high'] + self.align_frames.dx[frame_index])
                    # Compute the frame quality and append it to the list for this alignment point.
                    alignment_point['frame_qualities'].append(
                        method(frame[y_low:y_high, x_low:x_high],
                               self.configuration.alignment_points_rank_pixel_stride))
        else:
            # Sampled-down Laplacians of all blurred frames have been computed in
            # "frames.add_monochrome". Cut out boxes around alignment points from those objects,
            # rather than computing new Laplacians. Cycle through all alignment points:
            for alignment_point in self.alignment_points:
                alignment_point['frame_qualities'] = []
                # Cycle through all frames. Use the blurred monochrome image for ranking.
                for frame_index, frame in enumerate(self.frames.frames_mono_blurred_laplacian):
                    # Compute patch bounds within the current frame.
                    y_low = int(max(0, alignment_point['patch_y_low'] + self.align_frames.dy[
                        frame_index]) / self.configuration.align_frames_sampling_stride)
                    y_high = int(min(self.frames.shape[0],
                                 alignment_point['patch_y_high'] + self.align_frames.dy[
                                     frame_index]) / self.configuration.align_frames_sampling_stride)
                    x_low = int(max(0, alignment_point['patch_x_low'] + self.align_frames.dx[
                        frame_index]) / self.configuration.align_frames_sampling_stride)
                    x_high = int(min(self.frames.shape[1],
                                 alignment_point['patch_x_high'] + self.align_frames.dx[
                                     frame_index]) / self.configuration.align_frames_sampling_stride)
                    # Compute the frame quality and append it to the list for this alignment point.
                    alignment_point['frame_qualities'].append(frame[y_low:y_high, x_low:x_high].var())

        # For each alignment point sort the computed quality ranks in descending order.
        for alignment_point_index, alignment_point in enumerate(self.alignment_points):
            alignment_point['best_frame_indices'] = [b[0] for b in sorted(
                enumerate(alignment_point['frame_qualities']), key=lambda i: i[1],
                reverse=True)]
            # Truncate the list to the number of frames to be stacked for each alignmeent point.
            alignment_point['best_frame_indices'] = alignment_point['best_frame_indices'][
                                                    :self.stack_size]
            # Add this alignment point to the AP lists of those frames where the AP is to be used.
            for frame_index in alignment_point['best_frame_indices']:
                self.frames.used_alignment_points[frame_index].append(alignment_point_index)

    def compute_shift_alignment_point(self, frame_index, alignment_point_index, de_warp=True):
        """
        Compute the [y, x] pixel shift vector at a given alignment point relative to the mean frame.
        Four different methods can be used to compute the shift values:
        - a subpixel algorithm from "skimage.feature"
        - a phase correlation algorithm (miscellaneous.translation)
        - a local search algorithm (spiralling outwards), see method "search_local_match",
          optionally with subpixel accuracy.
        - a local search algorithm, based on steepest descent, see method
          "search_local_match_gradient". This method is faster than the previous one, but it has no
          subpixel option.

        Be careful with the sign of the local shift values. For the first two methods, a positive
        value means that the current frame has to be shifted in the positive coordinate direction
        in order to make objects in the frame match with their counterparts in the reference frame.
        In other words: If the shift is positive, an object in the current frame is at lower pixel
        coordinates as compared to the reference. This is very counter-intuitive, but to make the
        three methods consistent, the same approach was followed in the implementation of the third
        method "search_local_match", contained in this module. There, a pixel box around an
        alignment point in the current frame is moved until the content of the box matches with the
        corresponding box in the reference frame. If at this point the box is shifted towards a
        higher coordinate value, this value is returned with a negative sign as the local shift.

        :param frame_index: Index of the selected frame in the list of frames
        :param alignment_point_index: Index of the selected alignment point
        :param de_warp: If True, include local warp shift computation. If False, only apply
                        global frame shift.
        :return: Local shift vector [dy, dx]
        """

        alignment_point = self.alignment_points[alignment_point_index]
        y_low = alignment_point['box_y_low']
        y_high = alignment_point['box_y_high']
        x_low = alignment_point['box_x_low']
        x_high = alignment_point['box_x_high']
        reference_box = alignment_point['reference_box']

        # The offsets dy and dx are caused by two effects: First, the mean frame is smaller
        # than the original frames. It only contains their intersection. And second, because the
        # given frame is globally shifted as compared to the mean frame.
        dy = self.align_frames.dy[frame_index]
        dx = self.align_frames.dx[frame_index]

        if de_warp:
            # Use subpixel registration from skimage.feature, with accuracy 1/10 pixels.
            if self.configuration.alignment_points_method == 'Subpixel':
                # Cut out the alignment box from the given frame. Take into account the offsets
                # explained above.
                box_in_frame = self.frames.frames_mono_blurred[frame_index][y_low + dy:y_high + dy,
                               x_low + dx:x_high + dx]
                shift_pixel, error, diffphase = register_translation(
                    reference_box, box_in_frame, 10, space='real')

            # Use a simple phase shift computation (contained in module "miscellaneous").
            elif self.configuration.alignment_points_method == 'CrossCorrelation':
                # Cut out the alignment box from the given frame. Take into account the offsets
                # explained above.
                box_in_frame = self.frames.frames_mono_blurred[frame_index][y_low + dy:y_high + dy,
                               x_low + dx:x_high + dx]
                shift_pixel = Miscellaneous.translation(reference_box,
                                                        box_in_frame, box_in_frame.shape)

            # Use a local search (see method "search_local_match" below.
            elif self.configuration.alignment_points_method == 'RadialSearch':
                shift_pixel, dev_r = Miscellaneous.search_local_match(
                    reference_box, self.frames.frames_mono_blurred[frame_index],
                    y_low + dy, y_high + dy, x_low + dx, x_high + dx,
                    self.configuration.alignment_points_search_width,
                    self.configuration.alignment_points_sampling_stride,
                    sub_pixel=self.configuration.alignment_points_local_search_subpixel)

            # Use the steepest descent search method.
            elif self.configuration.alignment_points_method == 'SteepestDescent':
                shift_pixel, dev_r = Miscellaneous.search_local_match_gradient(
                    reference_box, self.frames.frames_mono_blurred[frame_index],
                    y_low + dy, y_high + dy, x_low + dx, x_high + dx,
                    self.configuration.alignment_points_search_width,
                    self.configuration.alignment_points_sampling_stride)
            else:
                raise NotSupportedError("The point shift computation method " +
                                        self.configuration.alignment_points_method +
                                        " is not implemented")

            # Return the computed shift vector.
            return shift_pixel
        else:
            # If no de-warping is computed, just return the zero vector.
            return [0, 0]

    def show_alignment_points(self, image):
        """
        Create an RGB version of a monochrome image and insert red crosses at all alignment
        point locations. Draw green alignment point boxes and white alignment point patches.

        :return: 8-bit RGB image with annotations.
        """

        if len(image.shape) == 3:
            color_image = image.astype(uint8)
        else:
            # Expand the monochrome reference frame to RGB
            color_image = stack((image.astype(uint8),) * 3, -1)

        # For all alignment boxes insert a color-coded cross.
        cross_half_len = 5

        for alignment_point in (self.alignment_points):
            y_center = alignment_point['y']
            x_center = alignment_point['x']
            Miscellaneous.insert_cross(color_image, y_center,
                                       x_center, cross_half_len, 'red')
            box_y_low = max(alignment_point['box_y_low'], 0)
            box_y_high = min(alignment_point['box_y_high'], image.shape[0]) - 1
            box_x_low = max(alignment_point['box_x_low'], 0)
            box_x_high = min(alignment_point['box_x_high'], image.shape[1]) - 1
            for y in arange(box_y_low, box_y_high):
                color_image[y, box_x_low] = [255, 255, 255]
                color_image[y, box_x_high] = [255, 255, 255]
            for x in arange(box_x_low, box_x_high):
                color_image[box_y_low, x] = [255, 255, 255]
                color_image[box_y_high, x] = [255, 255, 255]

            patch_y_low = max(alignment_point['patch_y_low'], 0)
            patch_y_high = min(alignment_point['patch_y_high'], image.shape[0]) -1
            patch_x_low = max(alignment_point['patch_x_low'], 0)
            patch_x_high = min(alignment_point['patch_x_high'], image.shape[1]) - 1
            for y in arange(patch_y_low, patch_y_high):
                color_image[y, patch_x_low] = [0, int(
                    (255+color_image[y, patch_x_low][1])/2.), 0]
                color_image[y, patch_x_high] = [0, int(
                    (255 + color_image[y, patch_x_high][1]) / 2.), 0]
            for x in arange(patch_x_low, patch_x_high):
                color_image[patch_y_low, x] = [0, int(
                    (255+color_image[patch_y_low, x][1])/2.), 0]
                color_image[patch_y_high, x] = [0, int(
                    (255 + color_image[patch_y_high, x][1]) / 2.), 0]

        return color_image

if __name__ == "__main__":
    # Images can either be extracted from a video file or a batch of single photographs. Select
    # the example for the test run.
    type = 'video'
    if type == 'image':
        names = glob.glob('Images/2012*.tif')
        # names = glob.glob('Images/Moon_Tile-031*ap85_8b.tif')
        # names = glob.glob('Images/Example-3*.jpg')
    else:
        names = 'Videos/short_video.avi'
    print(names)

    # Get configuration parameters.
    configuration = Configuration()
    try:
        frames = Frames(configuration, names, type=type)
        print("Number of images read: " + str(frames.number))
        print("Image shape: " + str(frames.shape))
    except Exception as e:
        print("Error: " + e.message)
        exit()

    # The whole quality analysis and shift determination process is performed on a monochrome
    # version of the frames. If the original frames are in RGB, the monochrome channel can be
    # selected via a configuration parameter. Add a list of monochrome images for all frames to
    # the "Frames" object.
    start = time()
    frames.add_monochrome(configuration.frames_mono_channel)
    end = time()
    print('Elapsed time in creating blurred monochrome images: {}'.format(end - start))

    # Rank the frames by their overall local contrast.
    rank_frames = RankFrames(frames, configuration)
    start = time()
    rank_frames.frame_score()
    end = time()
    print('Elapsed time in ranking images: {}'.format(end - start))
    print("Index of maximum: " + str(rank_frames.frame_ranks_max_index))
    print("Frame scores: " + str(rank_frames.frame_ranks))
    print("Frame scores (sorted): " + str(
        [rank_frames.frame_ranks[i] for i in rank_frames.quality_sorted_indices]))
    print("Sorted index list: " + str(rank_frames.quality_sorted_indices))

    # Initialize the frame alignment object.
    align_frames = AlignFrames(frames, rank_frames, configuration)

    if configuration.align_frames_mode == "Surface":
        start = time()
        # Select the local rectangular patch in the image where the L gradient is highest in both x
        # and y direction. The scale factor specifies how much smaller the patch is compared to the
        # whole image frame.
        (x_low_opt, x_high_opt, y_low_opt, y_high_opt) = align_frames.select_alignment_rect(
            configuration.align_frames_rectangle_scale_factor)
        end = time()
        print('Elapsed time in computing optimal alignment rectangle: {}'.format(end - start))
        print("optimal alignment rectangle, x_low: " + str(x_low_opt) + ", x_high: " + str(
            x_high_opt) + ", y_low: " + str(y_low_opt) + ", y_high: " + str(y_high_opt))
        reference_frame_with_alignment_points = align_frames.frames_mono[
            align_frames.frame_ranks_max_index].copy()
        reference_frame_with_alignment_points[y_low_opt,
        x_low_opt:x_high_opt] = reference_frame_with_alignment_points[y_high_opt - 1,
                                x_low_opt:x_high_opt] = 255
        reference_frame_with_alignment_points[y_low_opt:y_high_opt,
        x_low_opt] = reference_frame_with_alignment_points[y_low_opt:y_high_opt, x_high_opt - 1] = 255
        # plt.imshow(reference_frame_with_alignment_points, cmap='Greys_r')
        # plt.show()

    # Align all frames globally relative to the frame with the highest score.
    start = time()
    align_frames.align_frames()
    end = time()
    print('Elapsed time in aligning all frames: {}'.format(end - start))
    print("Frame shifts: " + str(align_frames.frame_shifts))
    print("Intersection: " + str(align_frames.intersection_shape))

    start = time()
    # Compute the reference frame by averaging the best frames.
    average = align_frames.average_frame()

    # Initialize the AlignmentPoints object. This includes the computation of the average frame
    # against which the alignment point shifts are measured.

    alignment_points = AlignmentPoints(configuration, frames, rank_frames, align_frames)
    end = time()
    print('Elapsed time in computing average frame: {}'.format(end - start))
    print("Average frame computed from the best " + str(
        align_frames.average_frame_number) + " frames.")
    # plt.imshow(align_frames.mean_frame, cmap='Greys_r')
    # plt.show()

    # Create alignment points, and show alignment point boxes and patches.
    alignment_points.create_ap_grid(average)
    print("Number of alignment points created: " + str(len(alignment_points.alignment_points)) +
          ", number of dropped aps (dim): " + str(
        len(alignment_points.alignment_points_dropped_dim)) +
          ", number of dropped aps (structure): " + str(
        len(alignment_points.alignment_points_dropped_structure)))
    color_image = alignment_points.show_alignment_points(average)

    plt.imshow(color_image)
    plt.show()

    # For each alignment point rank frames by their quality.
    start = time()
    alignment_points.compute_frame_qualities()
    end = time()
    print('Elapsed time in ranking frames for every alignment point: {}'.format(end - start))

    y_low = 490
    y_high = 570
    x_low = 880
    x_high = 960
    found_ap_list = alignment_points.find_alignment_points(y_low, y_high, x_low, x_high)
    print ("Removing alignment points between bounds " + str(y_low) + " <= y <= " + str(y_high) +
           ", " + str(x_low) + " <= x <= " + str(x_high) + ":")
    for ap in found_ap_list:
        print ("y: " + str(ap['y']) + ", x: " + str(ap['x']))
        alignment_points.remove_alignment_point(ap)

    y_new = 530
    x_new = 920
    half_box_width_new = 40
    half_patch_width_new = 50
    num_pixels_y = average.shape[0]
    num_pixels_x = average.shape[1]
    alignment_points.alignment_points.append(
        alignment_points.new_alignment_point(average, frames.color, y_new, x_new,
                                             half_box_width_new,
                                             half_patch_width_new, num_pixels_y, num_pixels_x,
                                             extend_x_low=False, extend_x_high=False))
    print ("Added alignment point at y: " + str(y_new) + ", x: " + str(x_new) + ", box size: "
           + str(2*half_box_width_new) + ", patch size: " + str(2*half_patch_width_new))
    print("Searching for neighbors of failed APs.")
    alignment_points.find_alignment_point_neighbors()

    # Show updated alignment point boxes and patches.
    print("Number of alignment points created: " + str(len(alignment_points.alignment_points)) +
          ", number of dropped aps (dim): " + str(
        len(alignment_points.alignment_points_dropped_dim)) +
          ", number of dropped aps (structure): " + str(
        len(alignment_points.alignment_points_dropped_structure)))
    color_image = alignment_points.show_alignment_points(average)

    plt.imshow(color_image)
    plt.show()
