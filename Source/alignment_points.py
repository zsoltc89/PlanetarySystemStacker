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
from numpy import arange, amax, stack, amin
from skimage.feature import register_translation

from align_frames import AlignFrames
from configuration import Configuration
from exceptions import WrongOrderingError, NotSupportedError
from frames import Frames
from miscellaneous import quality_measure, insert_cross, circle_around
from rank_frames import RankFrames


class AlignmentPoints(object):
    def __init__(self, configuration, frames, rank_frames, align_frames):
        self.configuration = configuration
        self.frames = frames
        self.rank_frames = rank_frames
        self.align_frames = align_frames
        self.y_locations = None
        self.x_locations = None
        self.alignment_boxes = None
        self.alignment_points = None

        self.average_frame_number = max(ceil(frames.number * configuration.average_frame_percent / 100.), 1)
        self.align_frames.average_frame(
            [self.frames.frames_mono[i] for i in self.rank_frames.quality_sorted_indices[:self.average_frame_number]],
            [self.align_frames.frame_shifts[i] for i in
             self.rank_frames.quality_sorted_indices[:self.average_frame_number]])

    def create_alignment_boxes(self, step_size, box_size):
        mean_frame = self.align_frames.mean_frame
        mean_frame_shape = mean_frame.shape
        box_size_half = int(box_size / 2)
        self.alignment_boxes = []
        self.alignment_boxes_coordinates = []
        self.alignment_boxes_structure = []
        self.alignment_boxes_max_brightness = []
        self.alignment_boxes_min_brightness = []

        self.y_locations = arange(box_size_half + self.configuration.alignment_point_search_width,
                                  mean_frame_shape[0] - box_size_half - self.configuration.alignment_point_search_width,
                                  step_size, dtype=int)
        self.x_locations = arange(box_size_half + self.configuration.alignment_point_search_width,
                                  mean_frame_shape[1] - box_size_half - self.configuration.alignment_point_search_width,
                                  step_size, dtype=int)
        for j, y in enumerate(self.y_locations):
            for i, x in enumerate(self.x_locations):
                y_low = y - box_size_half
                y_high = y + box_size_half
                x_low = x - box_size_half
                x_high = x + box_size_half
                box = mean_frame[y_low:y_high, x_low:x_high]
                alignment_box = {}
                alignment_box['box'] = box
                alignment_box['coordinates'] = (j, i, y, x, y_low, y_high, x_low, x_high)
                alignment_box['structure'] = quality_measure(box)
                alignment_box['max_brightness'] = amax(box)
                alignment_box['min_brightness'] = amin(box)
                self.alignment_boxes.append(alignment_box)
        structure_max = max(alignment_box['structure'] for alignment_box in self.alignment_boxes)
        for alignment_box in self.alignment_boxes:
            alignment_box['structure'] /= structure_max

    def select_alignment_points(self, structure_threshold, brightness_threshold, contrast_threshold):
        if self.alignment_boxes == None:
            raise WrongOrderingError("Attempt to select alignment points before alignment boxes are created")
        self.alignment_points = [[box_index, coordinates] for [box_index, coordinates] in
                                 enumerate(box['coordinates'] for box in self.alignment_boxes)
                                 if
                                 self.alignment_boxes[box_index]['structure'] > structure_threshold and
                                 self.alignment_boxes[box_index]['max_brightness'] > brightness_threshold and
                                 self.alignment_boxes[box_index]['max_brightness'] -
                                 self.alignment_boxes[box_index]['min_brightness'] > contrast_threshold]

    def compute_alignment_point_shifts(self, frame_index):
        if self.alignment_points == None:
            raise WrongOrderingError("Attempt to compute alignment point shifts before selecting alingment points")
        point_shifts = []
        diffphases = []
        errors = []
        for point_index, [box_index, [j, i, y_center, x_center, y_low, y_high, x_low, x_high]] in enumerate(
                self.alignment_points):
            dy = self.align_frames.intersection_shape[0][0] - self.align_frames.frame_shifts[frame_index][0]
            dx = self.align_frames.intersection_shape[1][0] - self.align_frames.frame_shifts[frame_index][1]
            box_in_frame = self.frames.frames_mono[frame_index][y_low + dy:y_high + dy, x_low + dx:x_high + dx]
            if self.configuration.alignment_point_method == 'Subpixel':
                shift_pixel, error, diffphase = register_translation(self.alignment_boxes[box_index]['box'],
                                                                     box_in_frame,
                                                                     10, space='real')
                diffphases.append(diffphase)
                errors.append(error)
            elif self.configuration.alignment_point_method == 'CrossCorrelation':
                shift_pixel = self.align_frames.translation(self.alignment_boxes[box_index]['box'], box_in_frame,
                                                            box_in_frame.shape)
            elif self.configuration.alignment_point_method == 'LocalSearch':
                shift_pixel = self.search_local_match(self.alignment_boxes[box_index]['box'],
                                                      self.frames.frames_mono[frame_index],
                                                      y_low + dy, y_high + dy, x_low + dx, x_high + dx,
                                                      self.configuration.alignment_point_search_width)
            else:
                raise NotSupportedError(
                    "The point shift computation method " + self.configuration.alignment_point_method + " is not implemented")
            point_shifts.append(shift_pixel)
        return point_shifts, errors, diffphases

    def search_local_match(self, reference_box, frame, y_low, y_high, x_low, x_high, search_width):
        deviation_min = 1000000
        dy_min = None
        dx_min = None
        dev = []
        for r in arange(search_width + 1):
            circle_r = circle_around(0, 0, r)
            deviation_min_r, dy_min_r, dx_min_r = 1000000, None, None
            for (dx, dy) in circle_r:
                deviation = abs(reference_box - frame[y_low - dy:y_high - dy, x_low - dx:x_high - dx]).sum()
                if deviation < deviation_min_r:
                    deviation_min_r, dy_min_r, dx_min_r = deviation, dy, dx
            dev.append(deviation_min_r)
            if deviation_min_r >= deviation_min:
                return [dy_min, dx_min]
            else:
                deviation_min, dy_min, dx_min = deviation_min_r, dy_min_r, dx_min_r
        # print("search local match unsuccessful: y_low: " + str(y_low) + ", x_low: " + str(x_low))
        # print(
        #     "search local match unsuccessful: y: " + str((y_high + y_low) / 2.) + ", x: " + str((x_high + x_low) / 2.))
        return [0, 0]


if __name__ == "__main__":
    type = 'video'
    if type == 'image':
        names = glob.glob('Images/2012*.tif')
        # names = glob.glob('Images/Moon_Tile-031*ap85_8b.tif')
        # names = glob.glob('Images/Example-3*.jpg')
    else:
        names = 'Videos/short_video.avi'
    print(names)

    configuration = Configuration()
    try:
        frames = Frames(names, type=type)
        print("Number of images read: " + str(frames.number))
        print("Image shape: " + str(frames.shape))
    except Exception as e:
        print("Error: " + e.message)
        exit()

    rank_frames = RankFrames(frames, configuration)
    start = time()
    rank_frames.frame_score()
    end = time()
    print('Elapsed time in ranking images: {}'.format(end - start))
    print("Index of maximum: " + str(rank_frames.frame_ranks_max_index))
    print("Frame scores: " + str(rank_frames.frame_ranks))
    print("Frame scores (sorted): " + str([rank_frames.frame_ranks[i] for i in rank_frames.quality_sorted_indices]))
    print("Sorted index list: " + str(rank_frames.quality_sorted_indices))

    align_frames = AlignFrames(frames, rank_frames, configuration)
    start = time()
    (x_low_opt, x_high_opt, y_low_opt, y_high_opt) = align_frames.select_alignment_rect(
        configuration.alignment_rectangle_scale_factor)
    end = time()
    print('Elapsed time in computing optimal alignment rectangle: {}'.format(end - start))
    print("optimal alignment rectangle, x_low: " + str(x_low_opt) + ", x_high: " + str(x_high_opt) + ", y_low: " + str(
        y_low_opt) + ", y_high: " + str(y_high_opt))
    reference_frame_with_alignment_points = align_frames.frames_mono[align_frames.frame_ranks_max_index].copy()
    reference_frame_with_alignment_points[y_low_opt, x_low_opt:x_high_opt] = reference_frame_with_alignment_points[
                                                                             y_high_opt - 1, x_low_opt:x_high_opt] = 255
    reference_frame_with_alignment_points[y_low_opt:y_high_opt, x_low_opt] = reference_frame_with_alignment_points[
                                                                             y_low_opt:y_high_opt, x_high_opt - 1] = 255
    # plt.imshow(reference_frame_with_alignment_points, cmap='Greys_r')
    # plt.show()

    start = time()
    align_frames.align_frames()
    end = time()
    print('Elapsed time in aligning all frames: {}'.format(end - start))
    print("Frame shifts: " + str(align_frames.frame_shifts))
    print("Intersection: " + str(align_frames.intersection_shape))

    start = time()
    alignment_points = AlignmentPoints(configuration, frames, rank_frames, align_frames)
    end = time()
    print('Elapsed time in computing average frame: {}'.format(end - start))
    print("Average frame computed from the best " + str(alignment_points.average_frame_number) + " frames.")
    # plt.imshow(align_frames.mean_frame, cmap='Greys_r')
    # plt.show()

    step_size = configuration.alignment_box_step_size
    box_size = configuration.alignment_box_size
    start = time()
    alignment_points.create_alignment_boxes(step_size, box_size)
    end = time()
    print('Elapsed time in alignment box creation: {}'.format(end - start))
    print("Number of alignment boxes created: " + str(len(alignment_points.alignment_boxes)))

    structure_threshold = configuration.alignment_point_structure_threshold
    brightness_threshold = configuration.alignment_point_brightness_threshold
    contrast_threshold = configuration.alignment_point_contrast_threshold
    print("Selection of alignment points, structure threshold: " + str(
        structure_threshold) + ", brightness threshold: " + str(brightness_threshold) + ", contrast threshold: " + str(
        contrast_threshold))
    start = time()
    alignment_points.select_alignment_points(structure_threshold, brightness_threshold, contrast_threshold)
    end = time()
    print('Elapsed time in alignment point selection: {}'.format(end - start))
    print("Number of alignment points selected: " + str(len(alignment_points.alignment_points)))

    start = time()
    reference_frame_with_alignment_points = stack((align_frames.frames_mono[align_frames.frame_ranks_max_index],) * 3,
                                                  -1)
    cross_half_len = 5
    for [index, [j, i, y_center, x_center, y_low, y_high, x_low, x_high]] in alignment_points.alignment_points:
        insert_cross(reference_frame_with_alignment_points, y_center, x_center, cross_half_len, 'white')
    end = time()
    print('Elapsed time in drawing alignment points: {}'.format(end - start))
    # plt.imshow(reference_frame_with_alignment_points)
    # plt.show()

    frame_index_details = 0
    y_center_low_details = 0
    y_center_high_details = 4000
    x_center_low_details = 0
    x_center_high_details = 6000
    warp_threshold = 0.1
    box_size_half = int(configuration.alignment_box_size / 2)

    for frame_index in range(frames.number):
        frame_with_shifts = reference_frame_with_alignment_points.copy()
        start = time()
        point_shifts, errors, diffphases = alignment_points.compute_alignment_point_shifts(frame_index)
        end = time()
        print("Elapsed time in computing point shifts for frame number " + str(frame_index) + ": " + str(end - start))
        for point_index, [index, [j, i, y_center, x_center, y_low, y_high, x_low, x_high]] in enumerate(
                alignment_points.alignment_points):
            if point_shifts[point_index][0] == None:
                insert_cross(frame_with_shifts, y_center, x_center, cross_half_len, 'green')
            else:
                insert_cross(frame_with_shifts, y_center + int(round(point_shifts[point_index][0])),
                             x_center + int(round(point_shifts[point_index][1])),
                             cross_half_len, 'red')
        plt.imshow(frame_with_shifts)
        plt.show()

        if frame_index == frame_index_details:
            reference_frame = reference_frame_with_alignment_points.copy()
            for point_index, [index, [j, i, y_center, x_center, y_low, y_high, x_low, x_high]] in enumerate(
                    alignment_points.alignment_points):
                if y_center_low_details <= y_center <= y_center_high_details and x_center_low_details <= x_center <= x_center_high_details:
                    reference_frame_box = reference_frame[y_center - box_size_half:y_center + box_size_half,
                                          x_center - box_size_half:x_center + box_size_half]
                    dy = align_frames.intersection_shape[0][0] - align_frames.frame_shifts[frame_index][0]
                    dx = align_frames.intersection_shape[1][0] - align_frames.frame_shifts[frame_index][1]
                    box_in_frame = stack((frames.frames_mono[frame_index],) * 3, -1)[
                                   y_center - box_size_half + dy:y_center + box_size_half + dy,
                                   x_center - box_size_half + dx:x_center + box_size_half + dx]
                    insert_cross(box_in_frame, box_size_half, box_size_half, cross_half_len, 'red')
                    if point_shifts[point_index][0] == None:
                        point_dy = point_dx = point_dy_int = point_dx_int = 0
                        color_cross = 'green'
                    else:
                        point_dy = point_shifts[point_index][0]
                        point_dx = point_shifts[point_index][1]
                        point_dy_int = int(round(point_dy))
                        point_dx_int = int(round(point_dx))
                        color_cross = 'red'
                    if max(abs(point_dy), abs(point_dx)) < warp_threshold and not point_shifts[point_index][0] == None:
                        continue
                    print("frame shifts: " + str(dy) + ", " + str(dx))
                    if configuration.alignment_point_method == 'Subpixel':
                        print("Point shifts: " + str(point_dy) + ", " + str(point_dx) + ", Error: "
                              + str(errors[point_index]) + ", diffphase: " + str(diffphases[point_index]))
                    else:
                        print("Point shifts: " + str(point_dy) + ", " + str(point_dx))

                    box_in_frame_shifted = stack((frames.frames_mono[frame_index],) * 3, -1)[
                                           y_center - box_size_half + dy - point_dy_int:y_center + box_size_half + dy - point_dy_int,
                                           x_center - box_size_half + dx - point_dx_int:x_center + box_size_half + dx - point_dx_int]
                    insert_cross(box_in_frame_shifted, box_size_half, box_size_half, cross_half_len, color_cross)
                    fig = plt.figure(figsize=(12, 6))
                    ax1 = plt.subplot(1, 3, 1)
                    ax2 = plt.subplot(1, 3, 2, sharex=ax1, sharey=ax1)
                    ax3 = plt.subplot(1, 3, 3, sharex=ax2, sharey=ax2)
                    ax1.imshow(reference_frame_box)
                    ax1.set_axis_off()
                    ax1.set_title('Reference frame, y :' + str(y_center) + ", x:" + str(x_center))
                    ax2.imshow(box_in_frame)
                    ax2.set_axis_off()
                    ax2.set_title('Frame, dy: ' + str(dy) + ", dx: " + str(dx))
                    ax3.imshow(box_in_frame_shifted)
                    ax3.set_axis_off()
                    ax3.set_title('De-warped, dy: ' + str(point_dy) + ", dx: " + str(point_dx))
                    plt.show()
