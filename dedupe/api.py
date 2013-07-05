#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
dedupe provides the main user interface for the library the
Dedupe class
"""

try:
    from json.scanner import py_make_scanner
    import json
except ImportError:
    from simplejson.scanner import py_make_scanner
    import simplejson as json
import itertools
import logging
import types
import pickle

import numpy

import dedupe
import dedupe.core as core
import dedupe.training as training
import dedupe.training_serializer as training_serializer
import dedupe.crossvalidation as crossvalidation
import dedupe.predicates as predicates
import dedupe.blocking as blocking
import dedupe.clustering as clustering
import dedupe.tfidf as tfidf
from dedupe.distance.affinegap import normalizedAffineGapDistance
from dedupe.distance.haversine import compareLatLong
from dedupe.distance.jaccard import compareJaccard

try:
    from collections import OrderedDict
except ImportError :
    from core import OrderedDict


class Dedupe:
    """
    Public methods:

    - `__init__`
    - `train`
    - `blockingFunction`
    - `goodThreshold`
    - `duplicateClusters`
    - `writeTraining`
    - `writeSettings`
    """

    # === `Dedupe.__init__` ===

    def __init__(self, init=None):
        """
        Load or initialize a data model.

        #### Example usage

            # initialize from a settings file
            deduper = dedupe.Dedupe('my_learned_settings')

        or

            # initialize from a defined set of fields
            fields = {'Site name': {'type': 'String'},
                      'Address':   {'type': 'String'},
                      'Zip':       {'type': 'String', 'Has Missing':True},
                      'Phone':     {'type': 'String', 'Has Missing':True},
                      }
            
            deduper = dedupe.Dedupe(fields)

        
        #### Keyword arguments
        
        `init`
        A field definition or a file location for a settings file.

        #### Additional detail
        A field definition is a dictionary where the keys are the fields
        that will be used for training a model and the values are the
        field specification

        Field types include

        - String

        A 'String' type field must have as its key a name of a field
        as it appears in the data dictionary and a type declaration
        ex. `{'Phone': {type: 'String'}}`

        Longer example of a field definition:


            fields = {'name':       {'type': 'String'},
                      'address':    {'type': 'String'},
                      'city':       {'type': 'String'},
                      'cuisine':    {'type': 'String'}
                      }


        Settings files are typically generated by saving the settings
        learned in a previous session. If you need details for this
        file see the method [`writeSettings`][[api.py#writesettings]].
        """

        if init.__class__ is dict and init:
            self.data_model = _initializeDataModel(init)
            self.predicates = None
        elif init.__class__ is str and init:
            (self.data_model,
             self.predicates) = self._readSettings(init)
        elif init:
            raise ValueError('Incorrect Input Type: must supply either a '
                             'field definition or a settings file.'
                             )
        else:

            raise ValueError('No Input: must supply either a field '
                             'definition or a settings file.'
                             )


        self.training_data = None
        self.training_pairs = None
        self.data_sample = None
        self.dupes = None
        self.training_encoder = training_serializer._to_json
        self.training_decoder = training_serializer.dedupe_decoder

        string_predicates = (predicates.wholeFieldPredicate,
                             predicates.tokenFieldPredicate,
                             predicates.commonIntegerPredicate,
                             predicates.sameThreeCharStartPredicate,
                             predicates.sameFiveCharStartPredicate,
                             predicates.sameSevenCharStartPredicate,
                             predicates.nearIntegersPredicate,
                             predicates.commonFourGram,
                             predicates.commonSixGram)

        tfidf_string_predicates = tuple([tfidf.TfidfPredicate(threshold)
                                         for threshold
                                         in [0.2, 0.4, 0.6, 0.8]])

        self.blocker_types = {'String' : (string_predicates
                                          + tfidf_string_predicates)}


    def _initializeTraining(self, training_file=None):
        """
        Loads labeled examples from file, if passed.

        Keyword arguments:
        training_file -- path to a json file of labeled examples

        """

        n_fields = len(self.data_model['fields'])

        training_dtype = [('label', 'i4'), ('distances', 'f4', (n_fields, ))]

        self.training_data = numpy.zeros(0, dtype=training_dtype)
        self.training_pairs = None

        if training_file:
            (self.training_pairs, self.training_data) = self._readTraining(training_file, self.training_data)

    # === Dedupe.train ===
    def train(self,
              data_sample,
              training_source=None):
        """
        Learn field weights from file of labeled examples or round of 
        interactive labeling

        Keyword arguments:
        data_sample -- a sample of record pairs
        training_source -- either a path to a file of labeled examples or
                           a labeling function


        In the sample of record_pairs, each element is a tuple of two
        records. Each record is, in turn, a tuple of the record's key and
        a record dictionary.

        In in the record dictionary the keys are the names of the
        record field and values are the record values.

        For example, a data_sample with only one pair of records,

        [
          (
           (854, {'city': 'san francisco',
                  'address': '300 de haro st.',
                  'name': "sally's cafe & bakery",
                  'cuisine': 'american'}),
           (855, {'city': 'san francisco',
                 'address': '1328 18th st.',
                 'name': 'san francisco bbq',
                 'cuisine': 'thai'})
           )
         ]

        The labeling function will be used to do active learning. The
        function will be supplied a list of examples that the learner
        is the most 'curious' about, that is examples where we are most
        uncertain about how they should be labeled. The labeling function
        will label these, and based upon what we learn from these
        examples, the labeling function will be supplied with new
        examples that the learner is now most curious about.  This will
        continue until the labeling function sends a message that we
        it is done labeling.
            
        The labeling function must be a function that takes two
        arguments.  The first argument is a sequence of pairs of
        records. The second argument is the data model.

        The labeling function must return two outputs. The function
        must return a dictionary of labeled pairs and a finished flag.

        The dictionary of labeled pairs must have two keys, 1 and 0,
        corresponding to record pairs that are duplicates or
        nonduplicates respectively. The values of the dictionary must
        be a sequence of records pairs, like the sequence that was
        passed in.

        The 'finished' flag should take the value False for active
        learning to continue, and the value True to stop active learning.

        i.e.

        labelFunction(record_pairs, data_model) :
            ...
            return (labeled_pairs, finished)

        For a working example, see consoleLabel in training

        Labeled example files are typically generated by saving the
        examples labeled in a previous session. If you need details
        for this file see the method writeTraining.
        """

        self.data_sample = data_sample

        if training_source.__class__ is not str and not isinstance(training_source, types.FunctionType):
            raise ValueError

        if training_source.__class__ is str:
            logging.info('reading training from file')
            if self.training_data is None :
                self._initializeTraining(training_source)

            (self.training_pairs, self.training_data) = self._readTraining(training_source, self.training_data)

        elif isinstance(training_source, types.FunctionType):

            if self.training_data is None :
                self._initializeTraining()

            (self.training_data, 
             self.training_pairs,
             self.data_model) = training.activeLearning(self.data_sample,
                                                        self.data_model,
                                                        training_source,
                                                        self.training_data,
                                                        self.training_pairs)

        n_folds = min(numpy.sum(self.training_data['label'])/3, 20)

        n_folds = min(max(2,
                          numpy.sum(self.training_data['label'])/3),
                      20)

        logging.info('%d folds', n_folds)

        alpha = crossvalidation.gridSearch(self.training_data,
                                           core.trainModel, 
                                           self.data_model, 
                                           k=n_folds)

        self.data_model = core.trainModel(self.training_data,
                                          self.data_model, 
                                          alpha)

        self._logLearnedWeights()

    def blockingFunction(self, ppc=1, uncovered_dupes=1):
        """
        Returns a function that takes in a record dictionary and
        returns a list of blocking keys for the record. We will
        learn the best blocking predicates if we don't have them already.

        Keyword arguments:
        ppc -- Limits the Proportion of Pairs Covered that we allow a
               predicate to cover. If a predicate puts together a fraction
               of possible pairs greater than the ppc, that predicate will
               be removed from consideration.

               As the size of the data increases, the user will generally
               want to reduce ppc.

               ppc should be a value between 0.0 and 1.0

        uncovered_dupes -- The number of true dupes pairs in our training
                           data that we can accept will not be put into any
                           block. If true true duplicates are never in the
                           same block, we will never compare them, and may
                           never declare them to be duplicates.

                           However, requiring that we cover every single
                           true dupe pair may mean that we have to use
                           blocks that put together many, many distinct pairs
                           that we'll have to expensively, compare as well.

        """

        if not self.predicates:
            self.predicates = self._learnBlocking(ppc, uncovered_dupes)

        blocker = blocking.Blocker(self.predicates)

        return blocker

    def goodThreshold(self, blocks, recall_weight=1.5):
        """
        Returns the threshold that maximizes the expected F score,
        a weighted average of precision and recall for a sample of
        blocked data. 

        Keyword arguments:
        blocks --        Sequence of tuples of records, where each
                         tuple is a set of records covered by a blocking
                         predicate

        recall_weight -- Sets the tradeoff between precision and
                         recall. I.e. if you care twice as much about
                         recall as you do precision, set recall_weight
                         to 2.
        """

        blocked_records = (block.values() for block in blocks)

        candidates = core.blockedPairs(blocked_records)

        field_distances = core.fieldDistances(candidates, self.data_model)
        probability = core.scorePairs(field_distances, self.data_model)

        probability.sort()
        probability = probability[::-1]

        expected_dupes = numpy.cumsum(probability)

        recall = expected_dupes / expected_dupes[-1]
        precision = expected_dupes / numpy.arange(1, len(expected_dupes) + 1)

        score = recall * precision / (recall + recall_weight ** 2 * precision)

        i = numpy.argmax(score)

        logging.info('Maximum expected recall and precision')
        logging.info('recall: %2.3f', recall[i])
        logging.info('precision: %2.3f', precision[i])
        logging.info('With threshold: %2.3f', probability[i])

        return probability[i]

    def duplicateClusters(self, blocks, threshold=.5):
        """
        Partitions blocked data and returns a list of clusters, where
        each cluster is a tuple of record ids

        Keyword arguments:
        blocks --     Sequence of tuples of records, where each
                      tuple is a set of records covered by a blocking
                      predicate
                                          
        threshold --  Number between 0 and 1 (default is .5). We will
                      only consider as duplicates record pairs as
                      duplicates if their estimated duplicate likelihood is
                      greater than the threshold.

                      Lowering the number will increase recall, raising it
                      will increase precision
                              

        """

        # Setting the cluster threshold this ways is not principled,
        # but seems to reliably help performance
        cluster_threshold = threshold * 0.7

        
        blocked_keys, blocked_records = core.split((block.keys(),
                                                    block.values())
                                                   for block in blocks)


        candidate_keys = core.blockedPairs(blocked_keys)
        candidate_records = core.blockedPairs(blocked_records)
        
        self.dupes = core.scoreDuplicates(candidate_keys,
                                          candidate_records,
                                          self.data_model,
                                          threshold)
        clusters = clustering.cluster(self.dupes, cluster_threshold)

        return clusters

    def _learnBlocking(self, eta, epsilon):
        """Learn a good blocking of the data"""

        confident_nonduplicates = training.semiSupervisedNonDuplicates(self.data_sample,
                                                                       self.data_model,
                                                                       sample_size=32000)

        self.training_pairs[0].extend(confident_nonduplicates)


        predicate_set = predicateGenerator(self.blocker_types, self.data_model)


        
        learned_predicates = dedupe.blocking.blockTraining(self.training_pairs,
                                                           predicate_set,
                                                           eta,
                                                           epsilon)

        return learned_predicates

    def _logLearnedWeights(self):
        """
        Log learned weights and bias terms
        """
        logging.info('Learned Weights')
        for (k1, v1) in self.data_model.items():
            try:
                for (k2, v2) in v1.items():
                    logging.info((k2, v2['weight']))
            except AttributeError:
                logging.info((k1, v1))

    # === writeSettings === 

    def writeSettings(self, file_name):
        """
        Write a settings file that contains the 
        data model and predicates

        Keyword arguments:
        file_name -- path to file
        """

        with open(file_name, 'w') as f:
            pickle.dump(self.data_model, f)
            pickle.dump(self.predicates, f)

    def _readSettings(self, file_name):
        with open(file_name, 'rb') as f:
            try:
                data_model = pickle.load(f)
                predicates = pickle.load(f)
            except KeyError :
                raise ValueError("The settings file doesn't seem to be in "
                                 "right format. You may want to delete the "
                                 "settings file and try again")

        return data_model, predicates


    def writeTraining(self, file_name):
        """
        Write to a json file that contains labeled examples

        Keyword arguments:
        file_name -- path to a json file
        """

        d_training_pairs = {}
        for (label, pairs) in self.training_pairs.iteritems():
            d_training_pairs[label] = [(dict(pair[0]), dict(pair[1])) for pair in pairs]

        with open(file_name, 'wb') as f:
            json.dump(d_training_pairs, f, default=self.training_encoder)



    def _readTraining(self, file_name, training_pairs):
        """Read training pairs from a file"""
        with open(file_name, 'r') as f:
            training_pairs_raw = json.load(f, cls=self.training_decoder)

        training_pairs = {0: [], 1: []}
        for (label, examples) in training_pairs_raw.iteritems():
            for pair in examples:
                training_pairs[int(label)].append((core.frozendict(pair[0]),
                                                   core.frozendict(pair[1])))

        training_data = training.addTrainingData(training_pairs,
                                                 self.data_model,
                                                 self.training_data)

        return (training_pairs, training_data)


def _initializeDataModel(fields):
    """Initialize a data_model with a field definition"""
    data_model = {}
    data_model['fields'] = OrderedDict()

    interaction_terms = {}

    for (k, v) in fields.iteritems():
        if v.__class__ is not dict:
            raise ValueError("Incorrect field specification: field "
                             "specifications are dictionaries that must "
                             "include a type definition, ex. "
                             "{'Phone': {type: 'String'}}"
                             )
        elif 'type' not in v:

            raise ValueError("Missing field type: field "
                             "specifications are dictionaries that must "
                             "include a type definition, ex. "
                             "{'Phone': {type: 'String'}}"
                             )
        elif v['type'] not in ['String',
                               'LatLong',
                               'Set',
                               'Custom',
                               'Interaction']:

            raise ValueError("Invalid field type: field "
                             "specifications are dictionaries that must "
                             "include a type definition, ex. "
                             "{'Phone': {type: 'String'}}"
                             )
        elif v['type'] == 'LatLong' :
            if 'comparator' in v :
                raise ValueError("Custom comparators can only be defined "
                                  "for fields of type 'Custom'")
            else :
                v['comparator'] = compareLatLong

        elif v['type'] == 'Set' :
            if 'comparator' in v :
                raise ValueError("Custom comparators can only be defined "
                                  "for fields of type 'Custom'")
            else :
                v['comparator'] = compareJaccard


        elif v['type'] == 'String' :
            if 'comparator' in v :
                raise ValueError("Custom comparators can only be defined "
                                 "for fields of type 'Custom'")
            else :
                v['comparator'] = normalizedAffineGapDistance

        elif v['type'] == 'Custom' and 'comparator' not in v :
            raise ValueError("For 'Custom' field types you must define "
                             "a 'comparator' fucntion in the field "
                             "definition. ")

        elif v['type'] == 'Interaction' :
            if 'Interaction Fields' in v :
                 for field in v['Interaction Fields'] :
                     if 'Has Missing' in fields[field] :
                         v.update({'Has Missing' : True})
                         break
            else :
                raise ValueError('No "Interaction Fields" defined')

            v.update({'weight': 0})
            interaction_terms[k] = v
            # We want the interaction terms to be at the end of of the
            # ordered dict so we'll add them after we finish
            # processing all the other fields
            continue
            
        

        data_model['fields'][k] = v


    data_model['fields'].update(interaction_terms)


    for k, v in data_model['fields'].items() :
        if 'Has Missing' in v :
             if v['Has Missing'] :
                 data_model['fields'][k + ': not_missing'] = {'weight' : 0,
                                                              'type'   : 'Missing Data'}
        else :
            data_model['fields'][k].update({'Has Missing' : False})
         


    data_model['bias'] = 0
    return data_model

def predicateGenerator(blocker_types, data_model) :
    predicate_set = []
    for record_type, predicate_functions in blocker_types.items() :
        fields = [field_name for field_name, details
                  in data_model['fields'].items()
                  if details['type'] == record_type]
        predicate_set.extend(list(itertools.product(predicate_functions, fields)))
    predicate_set = disjunctivePredicates(predicate_set)

    return predicate_set


def disjunctivePredicates(predicate_set):

    disjunctive_predicates = list(itertools.combinations(predicate_set, 2))

    # filter out disjunctive predicates that operate on same field

    disjunctive_predicates = [predicate for predicate in disjunctive_predicates 
                              if predicate[0][1] != predicate[1][1]]

    predicate_set = [(predicate, ) for predicate in predicate_set]
    predicate_set.extend(disjunctive_predicates)

    return predicate_set
