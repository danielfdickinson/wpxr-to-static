#!/usr/bin/env python3

# Standard libraries
import codecs
import collections
import datetime
from glob import glob
import io
import logging
import os
import re
from shutil import copyfile
import sys

# Parsing and serializing
from html5lib import parseFragment as html5lib_parse, serialize as html5lib_serialize
from urllib.parse import urlparse, urljoin
from markdownify import markdownify
import toml
from urllib3 import PoolManager
from xml.etree.ElementTree import ElementTree, TreeBuilder, XMLParser, ParseError
import yaml

"""
wpxr-to-static - Wordpress XML exports to static website generator files

Copyright 2021 Daniel F. Dickinson

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

---------------------------------------------------------------------------

Tested with Wordpress 5.7 and Hugo 0.81.0 and 0.82.0

Tested with Python 3.9.4 (Windows) and 3.8.6 (Ubuntu Linux)

"""


class W2SConfig:

    BASE_CONFIG_YAML = """
# Logging level (just stderr output at the moment)
# Logs only this level of message and above through the log facility
loglevel: ERROR

# The wordpress export xml (WPXR) filename.
wpxr_file: wpxr/wpxr.xml

# The target directory where all output is saved.
build_dir: build

# Subdir of target dir for the content
content_dir: content

# Download image files from content bodies
download_content_images: false

# Relative path to your WordPress images
# e.g. /wp-content/uploads would create a match for
# an image in you WPXR with a URL such as
# https://www.example.com/wp-content/uploads/2021/01/an-image-file.png
# If www.example.com were the site's base URL.
image_origin_rel_url: /wp-content/uploads

# Where to find your WordPress images on your local filesystem
# Assumes you have copied the to your local system, for example
# using rclone, scp, or other network copy tool, or that you have
# download_content_images set to true (which will download to this location)
# If empty wpxr-to-static will not try to move the image
# files into the site's build (output) folder
image_origin_local_path:

# Where in the output (build) folder for the site to put the files
# (maintains the same file structure as the source except that the
# site baseURL and WP relative path are removed.
image_destination_path: static/images

# relative path to WorldPress images when live:
image_rel_url: /images

# Output extension
target_extension: md

# Item types we don't want to keep
item_type_filter:
  - attachment
  - custom_css
  - flamingo_contact
  - flamingo_inbound
  - nav_menu_item
  - wp_block
  - wpcf7_contact_form

# Rename fields
rename_fields:
  attached_file: _wp_attached_file
  attachment_alt: _wp_attachment_image_alt
  categories: category
  featured_image: _thumbnail_id
  layout: _wp_page_template
  number_edits: _edit_last
  old_slug: _wp_old_slug
  tags: post_tag

# Remove fields we don't want in final output, but do during processing
final_remove_fields:
  - _customize_changeset_uuid
  - _wp_attachment_data
  - attached_file
  - attachment_alt
  - featured_image
  - layout
  - number_edits
  - old_slug
  - parent
  - postmeta
  - slug
  - taxonomy
  - type

# Don't emit wp_id in output files
no_output_wp_id: true

# Options for converting HTML content to Markdown using markdownify
# markdownify:
heading_style: ATX
strip: ["script"]

# Remove single values we don't want, for a given field (list_of_maps)
remove_field_values:
  - aliases: /
  - draft: false

# Remove aliases that match permalink (i.e. duplicate path)
remove_permalink_alias: True

# Instead of login name use display name in Hugo author per-page metadata
use_author_display_name_in_metadata: True

# For items of wp:post_type page keep the page hierarchy
# (creates a directory structure to match the exiting page hierarchy)
keep_page_hierarchy: True

# Config file containing data models for converting the
# WPXR file to desired static generator
# May be yaml or toml
data_models: hugo_data_model.yaml
"""

    CONFIG_BASE = "base"
    CONFIG_MAIN = "main"
    CONFIG_DATA_MODEL = "data_model"

    def __init__(self, config_file_name):
        self.config = {}

        base_config_file = io.StringIO(W2SConfig.BASE_CONFIG_YAML)
        self.config[W2SConfig.CONFIG_BASE] = yaml.safe_load(base_config_file)
        base_config_file.close()

        self.read_config_file(W2SConfig.CONFIG_MAIN, config_file_name)

        if self.get_config_item("data_models") is not None:
            self.read_config_file(
                W2SConfig.CONFIG_DATA_MODEL, self.get_config_item("data_models")
            )
        else:
            # If no data_models filename is supplied, the the main config file
            # will double as as the data_models file (and must therefore contain
            # the data_models.
            # Normally the data model is independent and the main config does not
            # override the data model, nor are there defaults for the data model
            self.config[W2SConfig.CONFIG_DATA_MODEL] = self.config[
                W2SConfig.CONFIG_MAIN
            ]

    # Configuration
    def read_config_file(self, config_section, config_name):
        config = None

        if config_section not in [
            W2SConfig.CONFIG_MAIN,
            W2SConfig.CONFIG_DATA_MODEL,
        ]:
            raise ValueError("Invalid config_section " + str(config_section) + " given")

        config_file = io.open(config_name, "r", encoding="utf-8")
        if os.path.splitext(config_name)[1] == ".yaml":
            config = yaml.safe_load(config_file)
        elif os.path.splitext(config_name)[1] == ".toml":
            config = toml.load(config_file)
        config_file.close()
        self.config[config_section] = config

    def get_config_item(self, key):
        value = None
        if self.config[W2SConfig.CONFIG_MAIN] is not None:
            value = self.config[W2SConfig.CONFIG_MAIN].get(key)
        if value is None:
            value = self.config[W2SConfig.CONFIG_BASE].get(key)
        return value

    def get_data_model_item(self, key):
        value = self.config[W2SConfig.CONFIG_DATA_MODEL].get(key)
        return value


class WPXR:
    # Create namespace map from xml
    class ns_tree_builder(TreeBuilder):
        def __init__(self):
            TreeBuilder.__init__(self)
            self.namespaces = {}

        def start_ns(self, prefix, uri):
            if prefix != "":
                self.namespaces[prefix] = uri

    def __init__(self, wpxr_file):
        self.ns = {}
        self.wpxr_tree = {}
        self.parse_wpxr(wpxr_file)

    # Parse a WordPress XML file into an ElementTree
    def parse_wpxr(self, wpxr_file):
        tree_builder = self.ns_tree_builder()
        parser = XMLParser(target=tree_builder)
        tree = ElementTree()
        logging.info("Reading: " + wpxr_file)
        root = tree.parse(wpxr_file, parser=parser)

        # Namespace map
        self.ns = tree_builder.namespaces

        self.wpxr_tree = root.find("channel")
        if self.wpxr_tree:
            logging.info("Found 'channel' in " + wpxr_file)
        else:
            raise ParseError("Missing channel")

    def get_wpxr_tree(self):
        return self.wpxr_tree

    def get_wp_ns(self):
        return self.ns


def unstring_int(value):
    result = value
    try:
        intresult = int(value)
    except ValueError as e:
        if not str(e).startswith("invalid literal for " + "int() with base 10:"):
            raise ValueError(e)
    else:
        result = intresult

    return result


class TreeConverter:
    def __init__(self, element_tree, ns, modifier_map):
        self.element_tree = element_tree
        self.ns = ns
        self.out_tree = []
        self.contains_dispatch_map = {
            "attr": self.find_item_use_attrs_from_data_model,
            "item": self.find_item_apply_data_model,
            "list": self.find_list_apply_data_model,
        }

        self.modifier_map = {
            "key-value": self.map_to_key_value,
            "list-up-map": self.list_up_map,
            "pull-single": self.pull_single_from_list,
            "remove-list": self.remove_list,
            "remove-zero": self.remove_zero,
            "to-lower": self.to_lower,
        }

        # Merge modifier maps from instantation
        if (modifier_map is not None) and isinstance(
            modifier_map, collections.abc.Mapping
        ):
            for mod_key, mod_name in modifier_map.items():
                self.modifier_map[mod_key] = modifier_map[mod_key]

    def pull_single_from_list(
        self, multi_list, result_tree, data_model, item_name, context
    ):
        out_tree = multi_list
        item = None

        singles = data_model.get("singles")

        if (singles is not None) and isinstance(singles, list):
            if multi_list is not None:
                for single in singles:
                    if (single is not None) and isinstance(
                        single, collections.abc.Mapping
                    ):
                        for single_key, single_index in single.items():
                            if (multi_list.get(single_key) is not None) and isinstance(
                                multi_list[single_key], list
                            ):
                                if single_index > len(multi_list[single_key]) - 1:
                                    logging.error(
                                        "Index "
                                        + str(single_index)
                                        + " not in list at "
                                        + str(context)
                                    )
                                    item = None
                                else:
                                    item = multi_list[single_key][single_index]

        if item is not None:
            if isinstance(item, collections.abc.Mapping):
                for item_key, item_value in item.items():
                    # Only add, don't overwrite
                    if out_tree.get(item_key) is None:
                        out_tree[item_key] = item_value

        return out_tree

    def map_to_key_value(self, cur_map, result_tree, data_model, item_name, context):
        out_map = cur_map
        new_map = {}
        key = data_model.get("key")
        value = data_model.get("value")
        if (key is not None) and (value is not None):
            key_value = cur_map.get(key)
            value_value = cur_map.get(value)
            if (key_value is not None) and (value_value is not None):
                new_map[key_value] = value_value
        if len(new_map) > 0:
            out_map = new_map

        return out_map

    def list_up_map(self, cur_map, result_tree, data_model, item_name, context):
        out_map = result_tree
        if isinstance(out_map, collections.abc.Mapping):
            if isinstance(cur_map, collections.abc.Mapping):
                for item_key, item_value in cur_map.items():
                    if out_map.get(item_key) is None:
                        out_map[item_key] = [item_value]
                    elif not isinstance(out_map[item_key], list):
                        out_map[item_key] = [out_map[item_key], item_value]
                    else:
                        out_map[item_key].append(item_value)
        return cur_map

    def remove_list(self, cur_map, result_tree, data_model, item_name, context):
        out_map = result_tree
        keys_to_delist = data_model.get("remove_list_keys")
        if (keys_to_delist is not None) and isinstance(keys_to_delist, list):

            if (out_map is None) or (not isinstance(out_map, collections.abc.Mapping)):
                logging.error(
                    "result_tree is not a map for remove_list at " + str(context)
                )
                return cur_map

            for delist_key in keys_to_delist:
                if (
                    (out_map.get(delist_key) is not None)
                    and isinstance(out_map[delist_key], list)
                    and len(out_map[delist_key]) == 1
                ):
                    out_map[delist_key] = out_map[delist_key][0]
        return cur_map

    def remove_zero(self, cur_item, result_tree, data_model, item_name, context):
        if (cur_item is None) or cur_item == 0:
            return None
        else:
            return cur_item

    def to_lower(self, item, result_tree, data_model, item_name, context):
        new_item = item
        if isinstance(item, str):
            new_item = item.lower()
        return new_item

    def apply_one_modifier_to_item(
        self, item, result_tree, modifier, mod_apply, data_model, item_name, context
    ):

        result = item

        if mod_apply is True:
            if modifier is not None:
                modifier_function = self.modifier_map.get(modifier)
                if (modifier_function is not None) and callable(modifier_function):
                    logging.debug(
                        "Applying modifier " + str(modifier) + " at " + str(context)
                    )
                    if result is not None:
                        result = modifier_function(
                            result,
                            result_tree,
                            data_model,
                            item_name,
                            str(context) + " once",
                        )

        return result

    def apply_modifier_map_to_item(
        self, item, result_tree, modifier, mod_apply, data_model, item_name, context
    ):
        result = item

        if modifier is not None:
            if isinstance(mod_apply, list):
                result = item
                for mod_list_item in mod_apply:
                    if result is not None:
                        result = self.apply_one_modifier_to_item(
                            result,
                            result_tree,
                            mod_list_item,
                            True,
                            data_model,
                            item_name,
                            str(context) + ": " + mod_list_item,
                        )
            elif mod_apply is True:
                result = self.apply_one_modifier_to_item(
                    result,
                    result_tree,
                    modifier,
                    mod_apply,
                    data_model,
                    item_name,
                    str(context) + ": " + modifier,
                )

        return result

    def apply_modifiers_to_item(
        self, item, result_tree, modifiers, data_model, item_name, context
    ):
        result = item

        if modifiers is not None:
            if isinstance(modifiers, collections.abc.Mapping):
                for modifier, mod_apply in modifiers.items():
                    result = self.apply_modifier_map_to_item(
                        result,
                        result_tree,
                        modifier,
                        mod_apply,
                        data_model,
                        item_name,
                        str(context) + ": apply(modifier)",
                    )

        return result

    def apply_modifiers_to_result(
        self,
        result,
        dispatch_modifiers,
        result_tree,
        dispatch_contained,
        dispatch_value,
        context,
    ):
        result_list = None
        base_result = result

        if len(dispatch_modifiers) > 0:
            if base_result is not None:
                if isinstance(base_result, list):
                    item_num = 0
                    for cur_res in base_result:
                        item_num = item_num + 1
                        cur_res = self.apply_modifiers_to_item(
                            cur_res,
                            result_tree,
                            dispatch_modifiers,
                            dispatch_contained,
                            dispatch_value,
                            str(context)
                            + " for item # "
                            + str(item_num)
                            + " in result list apply modifiers",
                        )
                        if cur_res is not None:
                            if result_list is not None:
                                result_list.append(cur_res)
                            else:
                                result_list = [cur_res]
                    result = result_list
                else:
                    if result is not None:
                        result = self.apply_modifiers_to_item(
                            result,
                            result_tree,
                            dispatch_modifiers,
                            dispatch_contained,
                            dispatch_value,
                            str(context) + " for item in result apply modifiers",
                        )

        return result

    def apply_contains_map_to_element(
        self,
        element,
        contains_item,
        contained,
        data_model,
        modifier,
        result_tree,
        context,
    ):
        dispatch_function = None
        dispatch_contained = None
        dispatch_modifiers = {}
        new_map = None
        dispatch_value = None
        dispatch_type = None

        for contains_type, contains_value in contains_item.items():
            if (contains_type is None) or (contains_value is None):
                logging.error("'contains' has an invalid entry at " + str(context))
                return

            if (self.contains_dispatch_map.get(contains_type) is not None) and callable(
                self.contains_dispatch_map[contains_type]
            ):
                dispatch_function = self.contains_dispatch_map[contains_type]
                if (contained is not None) and (
                    contained.get(contains_value) is not None
                ):
                    dispatch_contained = contained[contains_value]
                    dispatch_value = contains_value
                    dispatch_type = contains_type
            else:
                dispatch_modifiers[contains_type] = contains_value

        if (dispatch_function is None) or (dispatch_contained is None):
            logging.critical(
                "'contains' does not have a valid dispatch value in " + str(context),
                exc_info=True,
            )
            return

        logging.debug(
            "Applying "
            + str(dispatch_type)
            + " to "
            + str(dispatch_value)
            + " at "
            + str(context),
        )

        result = dispatch_function(
            element,
            dispatch_contained,
            modifier,
            result_tree,
            str(context)
            + " dispatch("
            + str(dispatch_type)
            + ":"
            + str(dispatch_value)
            + ")",
        )

        if result is not None:
            result = self.apply_modifiers_to_result(
                result,
                dispatch_modifiers,
                result_tree,
                dispatch_contained,
                dispatch_value,
                str(context) + " apply_modifiers",
            )

        if result is not None:
            if isinstance(result_tree, list):
                if new_map is None:
                    new_map = {}
                new_map[dispatch_value] = result
            else:
                logging.debug(
                    "Adding " + str(dispatch_value) + " at " + str(context),
                )
                result_tree[dispatch_value] = result

        if new_map is not None:
            result_tree.append(new_map)

    def apply_contains_to_element_for_result_tree(
        self, element, data_model, modifier, result_tree, context
    ):
        if (data_model is not None) and isinstance(data_model, collections.abc.Mapping):
            if data_model.get("contains") is not None:
                if not isinstance(data_model["contains"], list):
                    logging.critical(
                        "'contains' is not a list in data "
                        + "model for an element in "
                        + str(context),
                        exc_info=True,
                    )
                    return

                if (data_model.get("contained") is None) or (
                    not isinstance(data_model["contained"], collections.abc.Mapping)
                ):
                    logging.critical(
                        "'contained' is not a map "
                        + "in data model for an element in "
                        + str(context),
                        exc_info=True,
                    )
                    return
            else:
                logging.critical(
                    "'data_model' is not a map which includes 'contains' in "
                    + str(context),
                    exc_info=True,
                )
                return

            contains = data_model["contains"]
            contained = data_model["contained"]

            for contains_item in contains:
                if (contains_item is None) or (
                    not isinstance(contains_item, collections.abc.Mapping)
                ):
                    logging.critical("'contains' has a non-map item in " + str(context))
                    return

                logging.debug("Applying contains at " + str(context))

                result = self.apply_contains_map_to_element(
                    element,
                    contains_item,
                    contained,
                    data_model,
                    modifier,
                    result_tree,
                    str(context) + ": contains",
                )

                if result is not None:
                    logging.critical(
                        "Unexpected result applying contains at " + str(context),
                        exc_info=True,
                    )
                    return
        else:
            return self.apply_data_model_to_element(
                element,
                data_model,
                modifier,
                result_tree,
                str(context) + " model_to_element",
            )

    def apply_data_model_to_element(
        self, element, data_model, modifier, result_tree, context
    ):
        result = None

        if element is None:
            return None

        if (
            (data_model is not None)
            and isinstance(data_model, collections.abc.Mapping)
            and (data_model.get("contains") is not None)
        ):

            if (result_tree is None) or (
                not isinstance(result_tree, collections.abc.Mapping)
            ):
                result_tree = {}

            self.apply_contains_to_element_for_result_tree(
                element,
                data_model,
                modifier,
                result_tree,
                str(context) + " map_contains",
            )
            result = result_tree
        else:
            result = element.text
            if result is not None:
                result = unstring_int(result)

            logging.debug("Got value for " + str(context))
            if (modifier is not None) and isinstance(modifier, collections.abc.Mapping):
                result = self.apply_modifiers_to_result(
                    result,
                    modifier,
                    result_tree,
                    data_model,
                    str(context) + " element modifiers",
                )

        return result

    def apply_data_model_to_list(
        self, element_list, data_model, modifier, result_tree, context
    ):
        result = None
        out_tree = {}
        out_list = []
        if (result_tree is not None) and isinstance(result_tree, list):
            out_list = result_tree

        if data_model is None:
            logging.critical(
                "Missing data_model in '" + str(context) + "'", exc_info=True
            )
            return result

        if (element_list is None) or (not isinstance(element_list, list)):
            logging.error(
                "Attempted to use _to_list data_model on non-list at " + str(context)
            )
            return result

        for item in element_list:
            result = self.apply_data_model_to_element(
                item,
                data_model,
                modifier,
                None,
                str(context) + " #" + str(element_list.index(item)),
            )
            if result is not None:
                out_list.append(result)

        return out_list

    def find_item_use_attrs_from_data_model(
        self, element_tree, data_model, modifier, result_tree, context
    ):
        result = None

        if data_model is not None:
            if isinstance(data_model, collections.abc.Mapping):
                result = {}
                self.for_map_apply_data_model(
                    element_tree,
                    data_model,
                    modifier,
                    result,
                    str(context) + " use_attrs",
                )

            else:
                if element_tree is not None:
                    result = element_tree.get(data_model)
                    if result is not None:
                        result = unstring_int(result)

        return result

    def find_item_apply_data_model(
        self, element_tree, data_model, modifier, result_tree, context
    ):
        result = None

        if data_model is not None:
            if isinstance(data_model, collections.abc.Mapping):
                result = {}
                self.for_map_apply_data_model(
                    element_tree,
                    data_model,
                    modifier,
                    result,
                    str(context) + " item_apply_map",
                )

            else:
                element = element_tree.find(data_model, self.ns)
                if element is not None:
                    context = context + ": " + data_model
                    result = self.apply_data_model_to_element(
                        element,
                        data_model,
                        modifier,
                        result_tree,
                        str(context) + " found item",
                    )

                else:
                    logging.debug(
                        "No value found for item "
                        + str(data_model)
                        + " at "
                        + str(context),
                    )

        return result

    def find_list_apply_data_model(
        self, element_tree, data_model, modifier, result_tree, context
    ):
        out_tree = None

        if (result_tree is None) or (not isinstance(result_tree, list)):
            out_tree = []
        else:
            out_tree = result_tree

        if data_model is not None:
            if isinstance(data_model, collections.abc.Mapping) and (
                data_model.get("no_tag") is not None
            ):
                logging.error(
                    "Can't have 'no_tag' for 'find_list_apply_data_model' at "
                    + str(context)
                )
                return
            else:
                element_list = None
                logging.debug(
                    "Finding list of elements and applying data model at "
                    + str(context),
                )
                if isinstance(data_model, collections.abc.Mapping) and (
                    data_model.get("tag") is not None
                ):
                    element_list = element_tree.findall(data_model["tag"], self.ns)
                    logging.debug(
                        "Applying data model to map tag " + str(data_model["tag"]),
                    )
                    context = context + ": " + data_model["tag"]
                else:
                    element_list = element_tree.findall(data_model, self.ns)
                    logging.debug("Applying data model to item " + str(data_model))
                    context = context + ": " + data_model
                if element_list is not None:
                    self.apply_data_model_to_list(
                        element_list,
                        data_model,
                        modifier,
                        out_tree,
                        str(context) + " got element_list",
                    )

        return out_tree

    def for_map_apply_data_model(
        self, element_tree, data_model, modifier, result_tree, context
    ):
        if result_tree is None:
            logging.error("Missing definition for 'result_tree' in " + str(context))
            return

        if (data_model is not None) and isinstance(data_model, collections.abc.Mapping):
            logging.debug("Applying map to element at " + str(context))
            self.apply_contains_to_element_for_result_tree(
                element_tree,
                data_model,
                modifier,
                result_tree,
                str(context) + " apply_map",
            )
        else:
            logging.error("Data model element is not a map at " + str(context))
            return


class HugoConverter:
    def __init__(self, config, wpxr_tree):
        self.config = config
        self.wpxr_tree = wpxr_tree
        self.site_url = None
        self.hugo_config = None
        self.content_map = {}
        self.hugo_items = None
        self.contents_checked = 0
        self.replacements = 0
        self.page_map = None
        self.image_paths = []
        self.original_image_urls = []

        self.modifier_map = {
            "author": self.sub_author_display_name_for_login_name,
            "content-hrefs": self.make_href_relative_in_content,
            "fields-value-replace": self.replace_value_in_fields,
            "from-wp-gmt-date": self.convert_from_wp_gmt_date,
            "image-urls-in-xml": self.handle_image_urls_in_html_content,
            "url": self.make_url_relative,
        }

        self.tree_converter = TreeConverter(
            self.wpxr_tree.get_wpxr_tree(),
            self.wpxr_tree.get_wp_ns(),
            self.modifier_map,
        )

        # Mangling
        self.use_author_display_name_in_metadata = (
            self.config.get_config_item("use_author_display_name_in_metadata") or False
        )

        self.fields_value_replace = (
            self.config.get_config_item("fields_value_replace") or {}
        )

        # Renaming fields
        self.rename_fields = self.config.get_config_item("rename_fields") or []

        # Removing fields, values
        self.field_filter = set(self.config.get_config_item("remove_fields") or [])
        self.remove_field_values = (
            self.config.get_config_item("remove_field_values") or []
        )

        # Image URL/Path configuration
        self.image_origin = self.config.get_config_item("image_origin_rel_url") or ""
        self.image_rel_url = self.config.get_config_item("image_rel_url") or "/images"

        # Data Models
        self.hugo_wp_items = config.get_data_model_item("hugo_wp_items")

        if (self.hugo_wp_items is None) or (
            not isinstance(self.hugo_wp_items, collections.abc.Mapping)
        ):
            raise ImportError("Invalid data_model for 'hugo_wp_items'")

        self.hugo_project_config = config.get_data_model_item("hugo_project_config")

        if (self.hugo_project_config is None) or (
            not isinstance(self.hugo_project_config, collections.abc.Mapping)
        ):
            raise ImportError(
                "Invalid definition of " + "'hugo_project_config' in config file"
            )

    def get_site_url(self):
        return self.site_url

    def get_hugo_config(self):
        if self.hugo_config is None:
            self.convert_hugo_config()
        return self.hugo_config

    def get_hugo_items(self):
        if self.hugo_config is None:
            self.convert_hugo_config()
        if self.hugo_items is None:
            self.convert_hugo_items()
        return self.hugo_items

    def get_content_map(self):
        if self.content_map is None:
            if self.hugo_config is None:
                self.convert_hugo_config()
            if self.hugo_items is None:
                self.convert_hugo_items()
        return self.content_map

    def get_original_image_urls(self):
        if len(self.original_image_urls) < 1:
            if self.hugo_config is None:
                self.convert_hugo_config()
            if self.hugo_items is None:
                self.convert_hugo_items()
        return self.original_image_urls

    def get_image_paths(self):
        if len(self.image_paths) < 1:
            if self.hugo_config is None:
                self.convert_hugo_config()
            if self.hugo_items is None:
                self.convert_hugo_items()
        return self.image_paths

    def get_page_map(self):
        if self.page_map is None:
            if self.hugo_items is None:
                self.get_hugo_items()
            self.build_page_map()
        return self.page_map

    # For absolute urls on this site, make URLs relative to site_url (baseURL)
    def make_url_relative(self, item_url, result_tree, item_map, item_name, context):
        if item_url is not None:
            item_parsed = urlparse(item_url)
            if self.site_url is not None:
                site_parsed = urlparse(self.site_url)
                if item_parsed.netloc == site_parsed.netloc:
                    item_parsed = item_parsed._replace(scheme="", netloc="")
            else:
                item_parsed = item_parsed._replace(scheme="", netloc="")
            return item_parsed.geturl()
        else:
            return item_url

    def replace_value_in_fields(self, item, result_tree, item_map, item_name, context):
        newcontent = str(item)
        if (
            (self.fields_value_replace is not None)
            and isinstance(self.fields_value_replace, collections.abc.Mapping)
            and (self.fields_value_replace.get(item_name) is not None)
            and isinstance(
                self.fields_value_replace[item_name], collections.abc.Mapping
            )
        ):
            field_replace_items = self.fields_value_replace[item_name]
            if item_name == "content":
                self.contents_checked = self.contents_checked + 1
            for target, replacement in field_replace_items.items():
                newcontent = re.sub(
                    target, replacement, newcontent, flags=re.MULTILINE | re.DOTALL
                )
                if (item != newcontent) and (item_name == "content"):
                    self.replacements = self.replacements + 1
                item = newcontent

        return newcontent

    def handle_image_urls_in_html_content(
        self, content, result_tree, item_map, item_name, context
    ):
        newcontent = str(content)
        html5_content = html5lib_parse(
            newcontent, container="div", namespaceHTMLElements=False
        )
        if html5_content is not None:
            elements_src_update = []
            gotFigure = False
            for element in html5_content.iter():
                if element.tag == "figure":
                    gotFigure = True
                elif element.tag == "img" and (gotFigure is True):
                    src = element.get("src")
                    if src is not None:
                        orig_src = src
                        parsed_src = urlparse(src)
                        parsed_site_url = urlparse(self.site_url)
                        new_src = src
                        if parsed_src.netloc == parsed_site_url.netloc:
                            # Strip the absolute origin and unwanted original path
                            new_src = src[
                                (len(urljoin(self.site_url, self.image_origin)) + 1) :
                            ]
                        # We did find a local URL
                        if src != new_src:
                            self.original_image_urls.append(orig_src)
                            self.image_paths.append(new_src)
                            new_src = urljoin(self.image_rel_url + "/", new_src)
                            elements_src_update.append(
                                {"element": element, "src": new_src}
                            )
                    gotFigure = False
                elif gotFigure is True:
                    gotFigure = False

            # Only rewrite content if we made one or more changes
            if len(elements_src_update) > 0:
                for element_src in elements_src_update:
                    element_src["element"].set("src", element_src["src"])
                newcontent = html5lib_serialize(
                    html5_content,
                    omit_optional_tags=False,
                    minimize_boolean_attributes=False,
                    use_trailing_solidus=True,
                )

        return newcontent

    # For absolute hrefs in content on this site, make URLs relative to site_url (baseURL)
    def make_href_relative_in_content(
        self, content, result_tree, item_map, item_name, context
    ):
        newcontent = str(content)
        html5_content = html5lib_parse(
            newcontent, container="div", namespaceHTMLElements=False
        )
        if html5_content is not None:
            elements_href_update = []
            gotFigure = False
            for element in html5_content.iter():
                if element.tag == "a":
                    href = element.get("href")
                    if href is not None:
                        href_parsed = urlparse(href)
                        if self.site_url is not None:
                            site_parsed = urlparse(self.site_url)
                            if href_parsed.netloc == site_parsed.netloc:
                                href_parsed = href_parsed._replace(scheme="", netloc="")
                                elements_href_update.append(
                                    {"element": element, "href": href_parsed.geturl()}
                                )
            if len(elements_href_update) > 0:
                for element_href in elements_href_update:
                    element_href["element"].set("href", element_href["href"])
                newcontent = html5lib_serialize(
                    html5_content,
                    omit_optional_tags=False,
                    minimize_boolean_attributes=False,
                    use_trailing_solidus=True,
                )
        return newcontent

    # Convert author to author_display_name, if requested
    def sub_author_display_name_for_login_name(
        self, author, result_tree, item_map, item_name, context
    ):
        if self.use_author_display_name_in_metadata:
            if (
                (author is not None)
                and (self.hugo_config["author"] is not None)
                and isinstance(self.hugo_config["author"], collections.abc.Mapping)
            ):
                page_author = self.hugo_config["author"]
                if (page_author.get("authors") is not None) and isinstance(
                    page_author["authors"], list
                ):
                    p_author_list = page_author["authors"]
                    for p_author in p_author_list:
                        if (p_author.get("uid") == author) and (
                            p_author.get("name") is not None
                        ):
                            author = p_author["name"]
        return author

    # Convert WP GMT date to iso-8601 format
    def convert_from_wp_gmt_date(self, date, result_tree, item_map, item_name, context):
        outdate = date
        olddate = datetime.datetime.strptime(str(date) + "Z", "%Y-%m-%d %H:%M:%S%z")
        outdate = olddate.isoformat()
        return outdate

    # Use the WP <-> config.toml data model to generate the
    # tree for a site's config.toml
    def convert_hugo_config(self):
        logging.info("Creating hugo config (e.g. toml file)")
        self.hugo_config = {}
        self.tree_converter.for_map_apply_data_model(
            self.wpxr_tree.get_wpxr_tree(),
            self.hugo_project_config,
            None,
            self.hugo_config,
            "hugo_config",
        )
        if (self.hugo_config is None) or len(self.hugo_config) < 1:
            raise Exception("Failed to create hugo_config")
        # Determine the site's base URL
        if self.hugo_config.get("baseURL") is not None:
            self.site_url = self.hugo_config["baseURL"]
        elif self.hugo_config.get("baseBlogURL") is not None:
            self.site_url = self.hugo_config["baseBlogURL"]
        elif self.hugo_config.get("homepage") is not None:
            self.site_url = self.hugo_config["homepage"]

        logging.info("Got baseURL of " + str(self.site_url))

    def convert_hugo_items(self):
        if self.site_url is None:
            raise AttributeError("No suitable base URL for the site has been defined.")

        logging.info("Finding list of items in hugo_wp_items and applying data model")
        # Create the base YaML tree, for adding metadata to individual files
        self.hugo_items = []
        self.tree_converter.find_list_apply_data_model(
            self.wpxr_tree.get_wpxr_tree(),
            self.hugo_wp_items,
            None,
            self.hugo_items,
            "hugo_items",
        )
        # Report how many replacement operations
        # we needed to perform.
        if self.replacements > 0:
            logging.info(
                "Did "
                + str(self.replacements)
                + " replacement(s) in "
                + str(self.contents_checked)
                + " content section(s)."
            )
        else:
            logging.info(
                "No replacements needed in "
                + str(self.contents_checked)
                + " content section(s)."
            )

    def page_map_add_parent(self, page_id, parent_id):
        if self.page_map.get(parent_id) is None:
            self.page_map[parent_id] = {"children": [page_id]}
        else:
            parents_children = self.page_map[parent_id].get("children")
            if parents_children is not None:
                if isinstance(parents_children, list):
                    if page_id not in parents_children:
                        parents_children.append(page_id)
                else:
                    raise TypeError(
                        "Page with id "
                        + str(parent_id)
                        + " has non-list for "
                        + "children id list"
                    )
            else:
                self.page_map[parent_id]["children"] = [page_id]

    def page_map_get_parent_path(self, page_id):
        cur_id = page_id
        slug_list = []
        parent_path = ""
        # Find top of this page's ancestry
        cur_id = self.page_map[cur_id].get("parent")
        while (
            ((cur_id is not None) and self.page_map.get(cur_id) is not None)
            and cur_id != 0
            and (self.page_map[cur_id].get("slug") is not None)
        ):
            slug_list.append(self.page_map[cur_id]["slug"])
            cur_id = self.page_map[cur_id]["parent"]

        if len(slug_list) > 0:
            reversed_slug_list = reversed(slug_list)
            parent_path = os.path.join(*reversed_slug_list)

        return parent_path

    def page_map_get_draft_status(self, page_id):
        cur_id = page_id
        draft_list = [self.page_map[cur_id].get("wp_status")]
        # Find top of this page's ancestry
        cur_id = self.page_map[cur_id].get("parent")
        while (
            (cur_id is not None) and self.page_map.get(cur_id) is not None
        ) and cur_id != 0:
            draft_list.append(self.page_map[cur_id].get("wp_status"))
            cur_id = self.page_map[cur_id]["parent"]

        draft = True
        last_draft = True
        if len(draft_list) > 0:
            reversed_draft_list = reversed(draft_list)
            for draft_status in reversed_draft_list:
                if draft_status == "publish":
                    draft = False
                elif draft_status == "inherit":
                    draft = last_draft
                else:
                    draft = True
                last_draft = draft

        return draft

    def build_page_map(self):
        self.page_map = {}

        # For items of type 'page' determine the parents and path
        for item in self.hugo_items:
            if (item.get("type") is not None) and item["type"] in [
                "page",
                "post",
                "posts",
            ]:
                page_id = item["wp_id"]
                page_index = self.hugo_items.index(item)
                wp_status = item.get("wp_status")
                if self.page_map.get(page_id) is None:
                    if item.get("parent"):
                        parent_id = item["parent"]
                    else:
                        parent_id = 0
                    self.page_map[page_id] = {
                        "children": [],
                        "page_index": page_index,
                        "parent": parent_id,
                        "slug": item["slug"],
                        "wp_status": wp_status,
                    }
                    self.page_map_add_parent(page_id, parent_id)
                else:
                    self.page_map[page_id]["page_index"] = page_index
                    self.page_map[page_id]["slug"] = item["slug"]
                    self.page_map[page_id]["wp_status"] = wp_status
                    if item.get("parent"):
                        parent_id = item["parent"]
                    else:
                        parent_id = 0
                    self.page_map[page_id]["parent"] = parent_id
                    self.page_map_add_parent(page_id, parent_id)

        for page_id in self.page_map.keys():
            self.page_map[page_id]["parent-path"] = self.page_map_get_parent_path(
                page_id
            )

    def mangle_hugo(self):
        content_count = 0
        items_found_content = []

        # Pull out content into a separate tree
        for item in self.hugo_items:
            if (item.get("content") is not None) and (item.get("wp_id") is not None):
                content_count = content_count + 1
                current_content = item.get("content")
                items_found_content.append(item)
                content = ""
                if isinstance(current_content, list):
                    for content_item in current_content:
                        content = str(content) + str(content_item)
                else:
                    content = current_content
                self.content_map[item["wp_id"]] = content

        for item in items_found_content:
            del item["content"]

        if self.page_map is None:
            self.page_map = self.get_page_map()

        # Rename fields
        keys_to_rename = self.rename_fields
        if (keys_to_rename is not None) and isinstance(
            keys_to_rename, collections.abc.Mapping
        ):

            for item in self.hugo_items:
                if (item is None) or (not isinstance(item, collections.abc.Mapping)):
                    logging.error(
                        "item is not a map for rename_keys at " + item["wp_id"]
                    )
                    continue

                renamed_keys = []

                for rename_key, rename_value in keys_to_rename.items():
                    if item.get(rename_value) is not None:
                        renamed_keys.append(rename_value)
                        item[rename_key] = item.get(rename_value)

                for rename_key in renamed_keys:
                    del item[rename_key]

        # Replace wp_status with draft: true or draft: false
        for page_id in self.page_map.keys():
            if self.page_map[page_id].get("page_index"):
                self.hugo_items[self.page_map[page_id]["page_index"]][
                    "draft"
                ] = self.page_map_get_draft_status(page_id)
                self.hugo_items[self.page_map[page_id]["page_index"]].pop("wp_status")

        # Remove unwanted values from fields (and field if empty)
        for item in self.hugo_items:
            if (self.remove_field_values is not None) and isinstance(
                self.remove_field_values, list
            ):
                for fields_values in self.remove_field_values:
                    if (fields_values is not None) and isinstance(
                        fields_values, collections.abc.Mapping
                    ):
                        remove_fields = []
                        for field, field_value in fields_values.items():
                            if item.get(field) is not None:
                                remove_values = []
                                if isinstance(item[field], list):
                                    if field_value in item[field]:
                                        remove_values.append(field_value)
                                    if len(item[field]) == len(remove_values):
                                        remove_fields.append(field)
                                elif item[field] == field_value:
                                    remove_fields.append(field)
                                if field not in remove_fields:
                                    for remove_value in remove_values:
                                        item[field].remove(remove_value)
                        for remove_field in remove_fields:
                            del item[remove_field]


class HugoWriter:
    def __init__(
        self,
        config,
        hugo_config,
        hugo_items,
        content_map,
        site_url,
        page_map,
        image_paths,
        original_image_urls,
    ):
        self.config = config
        self.site_url = site_url
        self.hugo_config = hugo_config
        self.hugo_items = hugo_items
        self.content_map = content_map
        self.image_paths = image_paths
        self.original_image_urls = original_image_urls

        # Files and Directories
        self.build_dir = self.config.get_config_item("build_dir") or "build"
        self.content_dir = self.config.get_config_item("content_dir") or "content"

        # General Configuration
        self.target_extension = self.config.get_config_item("target_extension") or ".md"
        self.markdownify_options = (
            self.config.get_config_item("markdownify_options") or {}
        )

        # Filtering items (pages/posts etc)
        self.item_type_filter = self.config.get_config_item("item_type_filter") or []

        self.item_field_filter = self.config.get_config_item("item_field_filter") or {}
        self.item_field_list_filter = (
            self.config.get_config_item("item_field_list_filter") or {}
        )

        # Remove fields needed during processing but not wanted in output
        self.final_remove_fields = (
            self.config.get_config_item("final_remove_fields") or []
        )

        # Remove fields via regexp that are not wanted in output
        self.regexp_remove_fields = (
            self.config.get_config_item("regexp_remove_fields") or []
        )

        # Don't output wp_id in metadata
        self.no_output_wp_id = self.config.get_config_item("no_output_wp_id") or False

        # Image handling

        self.download_content_images = (
            self.config.get_config_item("download_content_images") or False
        )

        self.image_origin_rel_url = (
            self.config.get_config_item("image_origin_rel_url") or ""
        )

        self.image_local_path = (
            self.config.get_config_item("image_origin_local_path") or None
        )

        self.image_destination_path = (
            self.config.get_config_item("image_destination_path") or "static/images"
        )

        # Handle aliases that match permalink (remove them)
        self.remove_permalink_alias = (
            self.config.get_config_item("remove_permalink_alias") or False
        )

        # Page Map
        self.page_map = page_map

    # Determine base path of site
    def set_base_path(self):
        name = str(self.site_url)
        name = re.sub("^https?", "", name)
        name = re.sub("[^A-Za-z0-9_.-]", "", name)
        base_dir = os.path.normpath(self.build_dir + "/" + name)
        if not os.path.exists(base_dir):
            os.makedirs(base_dir)
        self.base_dir = base_dir

    # Determine content path of site
    def set_content_path(self):
        if self.base_dir is None:
            self.set_base_path()
        base_dir = self.base_dir
        content_dir = os.path.join(base_dir, self.content_dir)
        if not os.path.exists(content_dir):
            os.makedirs(content_dir)
        self.site_content_dir = content_dir

    # Determine full path to dir
    # (and create if necessary) relative
    # to current working directory
    def get_full_dir(self, new_dir):
        if self.site_content_dir is None:
            self.set_content_path()
        full_dir = os.path.normpath(self.site_content_dir + "/" + new_dir)
        if not os.path.exists(full_dir):
            os.makedirs(full_dir)
        return full_dir

    def write_hugo_config_toml(self):
        logging.info("EMIT: config: config.toml")

        # Create the base output directory for the site
        self.set_base_path()

        # Dump the config.toml data model to
        # an actual config.toml for the site
        config_toml_file = codecs.open(
            os.path.join(self.base_dir, "config.toml"),
            "w",
            encoding="utf-8",
        )
        toml.dump(self.hugo_config, config_toml_file)
        config_toml_file.close()

    # Filter out files we don't want to emit
    def filter_items(self, item):
        skip_items = {}

        if item["type"] in self.item_type_filter:
            skip_items[item["wp_id"]] = "item_type_filter"
        for field, value in self.item_field_filter.items():
            if item.get(field) == value:
                skip_items[item["wp_id"]] = "item_field_filter"
        for field, value_list in self.item_field_list_filter.items():
            if (value_list is not None) and isinstance(value_list, list):
                if (item.get(field) is not None) and isinstance(item[field], list):
                    for value in value_list:
                        for item_value in item[field]:
                            if value == item_value:
                                skip_items[item["wp_id"]] = "item_field_list_filter"

        return skip_items

    # Convert from ElementTree to output files (yaml + markdown by default)
    def write_hugo_items(self):
        # Create the base output directory for the site
        self.set_content_path()

        logging.info("Processing found hugo_items")
        # Output the YaML metadata and content data
        # for the final output files for the pages and posts
        for item in self.hugo_items:
            skip_items = self.filter_items(item)

            item_rel_path = str(item["type"])
            new_item_rel_path = item_rel_path

            if item["wp_id"] in skip_items.keys():
                logging.info(
                    "SKIP: item: "
                    + os.path.normpath(item_rel_path + "/" + str(item["slug"]))
                    + " due to "
                    + str(skip_items[item["wp_id"]])
                )
            else:
                logging.info(
                    "EMIT: item: "
                    + os.path.normpath(new_item_rel_path + "/" + str(item["slug"]))
                )

            if item["wp_id"] not in skip_items.keys():
                item_rel_path = new_item_rel_path

                item_full_path = None

                if item["type"] != "page":
                    # Create the directory for the
                    # new files and output the content files
                    item_full_dir = self.get_full_dir(item_rel_path)
                    item_full_path = os.path.normpath(
                        item_full_dir
                        + "/"
                        + str(item["slug"])
                        + "."
                        + str(self.target_extension)
                    )
                else:
                    children = self.page_map[item["wp_id"]].get("children")
                    if (children is not None) and len(children) > 0:
                        item_full_dir = self.get_full_dir(
                            os.path.normpath(
                                self.page_map[item["wp_id"]]["parent-path"]
                                + "/"
                                + str(item["slug"])
                            )
                        )
                        item_full_path = os.path.normpath(
                            item_full_dir + "/_index" + "." + str(self.target_extension)
                        )
                    else:
                        item_full_dir = self.get_full_dir(
                            self.page_map[item["wp_id"]]["parent-path"]
                        )
                        item_full_path = os.path.normpath(
                            item_full_dir
                            + "/"
                            + str(item["slug"])
                            + "."
                            + str(self.target_extension)
                        )

                # Remove aliases which match the permalink
                if self.remove_permalink_alias is True:
                    permaslug = None
                    if item.get("type") == "page":
                        if self.page_map.get(item["wp_id"]):
                            permaslug = self.page_map[item["wp_id"]].get("parent-path")
                            if (permaslug is not None) and item.get("slug") is not None:
                                permaslug = permaslug + "/" + item["slug"]
                            else:
                                permaslug = item.get("slug")
                    else:
                        permaslug = item.get("slug")
                    aliases_remove = False
                    if (
                        (permaslug is not None)
                        and (item.get("aliases") is not None)
                        and isinstance(item["aliases"], list)
                    ):
                        alias_to_remove = []
                        for alias in item["aliases"]:
                            if alias.strip("/") == permaslug.strip("/"):
                                alias_to_remove.append(alias)
                        for alias in alias_to_remove:
                            item["aliases"].remove(alias)
                        if len(item["aliases"]) < 1:
                            aliases_remove = True
                    if aliases_remove == True:
                        del item["aliases"]

                wp_id = item["wp_id"]
                if self.no_output_wp_id is True:
                    del item["wp_id"]

                if (self.final_remove_fields is not None) and isinstance(
                    self.final_remove_fields, list
                ):
                    remove_field_keys = []
                    for field in self.final_remove_fields:
                        if item.get(field) is not None:
                            remove_field_keys.append(field)

                    for remove_field in remove_field_keys:
                        del item[remove_field]

                if (self.regexp_remove_fields is not None) and isinstance(
                    self.regexp_remove_fields, list
                ):
                    remove_field_keys = []
                    item_keys = item.keys()
                    for field in self.regexp_remove_fields:
                        for item_key in item_keys:
                            if re.search(field, item_key) is not None:
                                remove_field_keys.append(item_key)
                    for remove_field in remove_field_keys:
                        del item[remove_field]

                logging.debug("      full_path: " + str(item_full_path))
                item_file = codecs.open(item_full_path, "w", encoding="utf-8")
                yaml.dump(data=item, stream=item_file, explicit_start=True)

                if self.no_output_wp_id is True:
                    item["wp_id"] = wp_id

                item_file.write("---\n")
                if item.get("wp_id") is not None:
                    content = self.content_map.get(item["wp_id"])
                    if content is not None:
                        parsed_content = markdownify(
                            content, **self.markdownify_options
                        )
                        item_file.write(parsed_content)
                    item_file.write("\n")
                item_file.close()

    def download_images(self):
        if (
            (self.download_content_images is True)
            and (self.original_image_urls is not None)
            and isinstance(self.original_image_urls, list)
            and (self.image_origin_rel_url is not None)
            and (self.image_local_path is not None)
        ):
            http_pool = PoolManager()
            if not os.path.exists(self.image_local_path):
                os.makedirs(self.image_local_path)
            for image_url in self.original_image_urls:
                logging.info("Attempting to download " + str(image_url))
                parsed_url = urlparse(image_url)
                dest_rel_path = ""
                parsed_site_url = urlparse(self.site_url)
                # Only handle URLs from our site (local)
                if parsed_url.netloc != parsed_site_url.netloc:
                    logging.debug(
                        parsed_site_url.netloc + " != " + parsed_site_url.netloc
                    )
                    continue
                # Only handle URLs from our base URL
                if not (parsed_url.path.startswith(parsed_site_url.path)):
                    logging.debug(
                        parsed_url.path + " does not start with " + parsed_site_url.path
                    )

                    continue
                dest_rel_path = parsed_url.path
                if parsed_site_url.path != "/" and parsed_site_url.path != "":
                    dest_rel_path = parsed_site_url.path[
                        len(parsed_site_url.path) + 1 :
                    ]
                if dest_rel_path.startswith(self.image_origin_rel_url):
                    dest_rel_path = dest_rel_path[len(self.image_origin_rel_url) + 1 :]
                logging.debug(
                    "Got final dest_rel_path of " + str(os.path.dirname(dest_rel_path))
                )
                dest_full_dir = os.path.normpath(
                    self.image_local_path + "/" + os.path.dirname(dest_rel_path)
                )
                if not os.path.exists(dest_full_dir):
                    os.makedirs(dest_full_dir)

                dest_filename = os.path.normpath(
                    dest_full_dir + "/" + os.path.basename(dest_rel_path)
                )

                logging.info(
                    "Downloading " + str(image_url) + " -> " + str(dest_filename)
                )

                request = http_pool.request("GET", image_url, preload_content=False)
                if request.status < 300:
                    out_file = io.open(
                        dest_filename,
                        "wb",
                    )

                    for chunk in request.stream(1024):
                        out_file.write(chunk)
                    request.release_conn()
                    out_file.close()
                else:
                    request.release_conn()
                    logging.error(
                        "Failed to download file "
                        + str(image_url)
                        + "; got HTTP Status "
                        + str(request.status)
                    )

    def copy_images(self):
        if (
            (self.image_paths is not None)
            and isinstance(self.image_paths, list)
            and (self.image_destination_path is not None)
            and (self.image_local_path is not None)
        ):
            for image in self.image_paths:
                if os.path.exists(
                    os.path.normpath(self.image_local_path + "/" + image)
                ):
                    dest_path = os.path.normpath(
                        self.base_dir + "/" + self.image_destination_path + "/" + image
                    )
                    dest_dir = os.path.dirname(dest_path)
                    if not os.path.exists(dest_dir):
                        os.makedirs(dest_dir)
                    src_path = os.path.normpath(self.image_local_path + "/" + image)
                    logging.info("Copying " + str(src_path) + " -> " + str(dest_path))
                    copyfile(src_path, dest_path)
                else:
                    logging.error(
                        "Image "
                        + str(os.path.normpath(self.image_local_path + "/" + image))
                        + " does not exist."
                    )


# When this code is used as a command line program, it's configuration is entirely
# based on yaml or toml configuration files (base config is either config.* in the working
# directory, or from a file whose name(possibly including path) is supplied on the command line)
def main():
    # Get base configuration
    config_file_name = None

    # Extremely basic command line -- either a config file name or nothing
    if len(sys.argv) > 2:
        sys.stderr.write(
            "ERROR: "
            + str(sys.argv[0])
            + " only accepts a config file name or no parameters.\n"
        )
        sys.exit(1)
    elif len(sys.argv) == 2:
        config_file_name = sys.argv[1]

    config_files = None
    if config_file_name is None:
        config_files = glob("config.*")
    if (config_files is not None) and isinstance(config_files, list):
        config_file_name = config_files[0]

    config = W2SConfig(config_file_name)

    if config is None:
        sys.stderr.write(
            "ERROR: Unable to find valid recognized config file (must be valid yaml or toml)\n"
        )
        sys.exit(1)

    # Setup logging (if any)
    loglevel = config.get_config_item("loglevel")
    if not isinstance(logging.getLevelName(loglevel), int):
        sys.stderr.write("Invalid loglevel '%s' specified\n" % str(loglevel))
        sys.exit(1)

    logging.basicConfig(level=loglevel)
    logging.info("Initial configuration complete")

    # Files and Directories
    wpxr_file = config.get_config_item("wpxr_file")

    try:
        logging.info("Parsing WordPress WPXR for " + wpxr_file)
        wpxr_tree = WPXR(wpxr_file)
        logging.info("Converting from xml for " + wpxr_file)
        hugo_converter = HugoConverter(config, wpxr_tree)
        hugo_converter.convert_hugo_config()
        hugo_converter.convert_hugo_items()
        hugo_converter.mangle_hugo()
        logging.info("Writing data for " + wpxr_file)
        hugo_writer = HugoWriter(
            config,
            hugo_converter.get_hugo_config(),
            hugo_converter.get_hugo_items(),
            hugo_converter.get_content_map(),
            hugo_converter.get_site_url(),
            hugo_converter.get_page_map(),
            hugo_converter.get_image_paths(),
            hugo_converter.get_original_image_urls(),
        )
        hugo_writer.write_hugo_config_toml()
        hugo_writer.write_hugo_items()
        hugo_writer.download_images()  # May be a noop
        hugo_writer.copy_images()  # May also be a noop
        logging.info("Writing complete for converted " + wpxr_file)

    except KeyboardInterrupt:
        sys.exit(1)

    logging.info("Supplied file converted.")


if __name__ == "__main__":
    main()
